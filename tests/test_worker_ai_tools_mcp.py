from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import sqlite3
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

from aiworkhub import quality_reviewer
from aiworkhub import platform_io
from aiworkhub import repository_state
from aiworkhub import source_graph
from aiworkhub import worker_ai_tools_mcp as worker_tools


def test_worker_tools_uses_canonical_platform_chmod_fd() -> None:
    assert worker_tools.chmod_fd is platform_io.chmod_fd
    assert "def chmod_fd" not in inspect.getsource(worker_tools)

    src_root = Path(worker_tools.__file__).resolve().parents[1]
    package_root = Path(worker_tools.__file__).resolve().parent
    probe = """
import importlib.abc
import json
import sys


class BlockPackagePlatformIoOnce(importlib.abc.MetaPathFinder):
    def __init__(self):
        self.blocked = False

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "aiworkhub.platform_io" and not self.blocked:
            self.blocked = True
            raise ImportError("forced package platform_io miss")
        return None


sys.meta_path.insert(0, BlockPackagePlatformIoOnce())
from aiworkhub import worker_ai_tools_mcp as module

facade = sys.modules["platform_io"]
backend = sys.modules["_platform_process"]
print(json.dumps({
    "facade": facade.__name__,
    "backend": backend.__name__,
    "facade_backend_identity": facade._platform_process is backend,
    "chmod_fd_identity": module.chmod_fd is facade.chmod_fd,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=src_root,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(src_root), str(package_root))),
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "facade": "platform_io",
        "backend": "_platform_process",
        "facade_backend_identity": True,
        "chmod_fd_identity": True,
    }


def test_generate_worker_runtime_writes_exact_private_kilo_config(tmp_path):
    home = tmp_path / "home"
    runtime = worker_tools.generate_worker_mcp_runtime(
        home=home,
        request_id="a" * 32,
        task_id="task-kilo",
        runner="grok-4.6",
        topic="code",
        repo=tmp_path / "repo",
        authority_repo=tmp_path / "authority",
        source_graph_targets=[],
        session_topic="code",
        package_import_root=tmp_path / "package-root",
        python_executable="/usr/bin/python3",
    )

    assert runtime.kilo_config_path == home / ".config" / "kilo" / "kilo.json"
    payload = json.loads(runtime.kilo_config_path.read_text(encoding="utf-8"))
    assert payload == {
        "mcp": {
            worker_tools.SERVER_NAME: {
                "type": "local",
                "command": [
                    "/usr/bin/python3",
                    "-m",
                    "aiworkhub.worker_ai_tools_mcp",
                ],
                "environment": runtime.env,
                "enabled": True,
            }
        }
    }
    if os.name == "posix":
        assert stat.S_IMODE(runtime.kilo_config_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(runtime.kilo_config_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE((home / ".config").stat().st_mode) == 0o700


def _ctx(
    runtime: Path,
    *,
    repo: Path,
    authority_repo: Path,
    packet_path: Path | None,
    task_id: str,
) -> worker_tools.WorkerToolContext:
    runtime.mkdir(parents=True, exist_ok=True)
    ledger = runtime / "audit.jsonl"
    ledger.write_text("", encoding="utf-8")
    key = runtime / "audit.key"
    key.write_bytes(b"k" * 32)
    return worker_tools.WorkerToolContext(
        task_id=task_id,
        runner="claude_sonnet5" if packet_path else "codex_worker",
        topic="quality_review" if packet_path else "implementation",
        request_id="b" * 32,
        repo=repo,
        authority_repo=authority_repo,
        source_graph_targets=(),
        session_topic="quality_review",
        audit_ledger_path=ledger,
        audit_hmac_key_path=key,
        quality_review_packet_path=packet_path,
    )


def _write_candidate(tmp_path: Path, *, index: int = 0) -> tuple[Path, Path, Path, dict]:
    """Create canonical/candidate worktrees plus a matching review packet."""
    canonical = tmp_path / f"canonical{index}"
    candidate = tmp_path / f"candidate{index}"
    runtime = tmp_path / f"runtime{index}"
    canonical.mkdir()
    candidate.mkdir()
    runtime.mkdir()
    (canonical / "module.py").write_text(
        f"def canonical_only_{index}():\n    return {index}\n", encoding="utf-8"
    )
    repository_state.bootstrap_repository(canonical)
    source_graph.build_index(canonical)
    candidate_file = candidate / "module.py"
    candidate_file.write_text(
        f"def canonical_only_{index}():\n    return {index}\n\n"
        f"def candidate_only_symbol_{index}():\n    return {index + 2}\n",
        encoding="utf-8",
    )
    packet = quality_reviewer.build_review_packet(
        request_id=f"target-request-{index}",
        task_id=f"TARGET_TASK_{index}",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes={
            "module.py": hashlib.sha256(candidate_file.read_bytes()).hexdigest()
        },
    )
    packet_path = runtime / "quality_review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    return canonical, candidate, runtime, packet


def test_quality_review_prewarm_accepts_policy_excluded_and_zero_row_partitions(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical-devrules"
    canonical.mkdir()
    (canonical / "base.py").write_text(
        "def canonical_devrules_base():\n    return True\n", encoding="utf-8"
    )
    repository_state.bootstrap_repository(canonical)
    source_graph.build_index(canonical)

    def prewarm_packet(
        name: str, files: dict[str, str], *, request_id: str, task_id: str
    ) -> tuple[dict, Path, Path]:
        candidate = tmp_path / name
        runtime = tmp_path / f"{name}-runtime"
        candidate.mkdir()
        runtime.mkdir()
        changed_hashes: dict[str, str] = {}
        for relative, content in files.items():
            path = candidate / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            changed_hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        packet = quality_reviewer.build_review_packet(
            request_id=request_id,
            task_id=task_id,
            claim_epoch=1,
            worker_provider="codex_cli",
            changed_path_hashes=changed_hashes,
        )
        packet_path = runtime / "quality_review_packet.json"
        packet_path.write_text(json.dumps(packet), encoding="utf-8")
        result = worker_tools.prewarm_quality_review_source_graph(
            packet_path, repo=candidate, authority_repo=canonical
        )
        assert result["ok"] is True
        return result, candidate, packet_path

    mixed, mixed_repo, mixed_packet = prewarm_packet(
        "candidate-devrules",
        {
            "src/aiworkhub/development_rules.py": (
                "def devrules_candidate_symbol():\n    return 'ready'\n"
            ),
            "scripts/check_development_rules.sh": "#!/bin/sh\nexit 0\n",
            "tests/development_rules_fixture.txt": "excluded test fixture\n",
            "config/development_rules.json": "{}\n",
            ".aiworkhub/config/development_rules.json": "{}\n",
        },
        request_id="devrules-request",
        task_id="DEVRULES_TASK",
    )
    mixed_db = Path(mixed["db_path"])
    admitted_paths = {
        "src/aiworkhub/development_rules.py",
        "scripts/check_development_rules.sh",
        "config/development_rules.json",
    }
    excluded_paths = {
        "tests/development_rules_fixture.txt",
        ".aiworkhub/config/development_rules.json",
    }
    with sqlite3.connect(str(mixed_db)) as conn:
        indexed = dict(conn.execute("SELECT file_path, source_hash FROM files"))
        assert set(indexed) == admitted_paths
        for relative in admitted_paths:
            assert indexed[relative] == hashlib.sha256(
                (mixed_repo / relative).read_bytes()
            ).hexdigest()
        for relative in excluded_paths:
            assert relative not in indexed

    mixed_ctx = _ctx(
        mixed_db.parent,
        repo=mixed_repo,
        authority_repo=canonical,
        packet_path=mixed_packet,
        task_id="REVIEW_DEVRULES_TASK",
    )
    worker_tools._CACHE.clear()
    query = worker_tools.source_graph_query(
        mixed_ctx, mode="function", query="devrules_candidate_symbol", budget=16
    )
    assert query["ok"] is True
    assert "devrules_candidate_symbol" in query["content"]

    empty, _, _ = prewarm_packet(
        "candidate-zero-row",
        {
            "tests/development_rules_fixture.txt": "not source\n",
            ".aiworkhub/config/development_rules.json": "{}\n",
        },
        request_id="zero-row-request",
        task_id="ZERO_ROW_TASK",
    )
    with sqlite3.connect(str(empty["db_path"])) as conn:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone() == (0,)


def test_quality_review_prewarm_verifies_candidate_bytes_before_build(
    tmp_path: Path,
) -> None:
    canonical, candidate, runtime, packet = _write_candidate(tmp_path)
    packet_path = runtime / "quality_review_packet.json"
    # Tamper the candidate bytes after the packet digest was computed; prewarm
    # must fail closed before building any index.
    (candidate / "module.py").write_text(
        "def tampered():\n    return -1\n", encoding="utf-8"
    )
    with pytest.raises(
        worker_tools.WorkerToolError,
        match="quality_review_candidate_hash_mismatch",
    ):
        worker_tools.prewarm_quality_review_source_graph(
            packet_path, repo=candidate, authority_repo=canonical
        )
    # No candidate overlay may have been published on the failed path.
    assert not list(runtime.glob("candidate_source_graph_*.sqlite"))


def test_quality_review_prewarm_publishes_atomically_without_temp_leftovers(
    tmp_path: Path,
) -> None:
    canonical, candidate, runtime, packet = _write_candidate(tmp_path)
    packet_path = runtime / "quality_review_packet.json"
    prewarm = worker_tools.prewarm_quality_review_source_graph(
        packet_path, repo=candidate, authority_repo=canonical
    )
    assert prewarm["ok"] is True
    assert prewarm["built"] is True
    assert prewarm["authority_source"] == "candidate_overlay"
    assert prewarm["authority_state"] == "quality_review_readonly"
    db_path = Path(prewarm["db_path"])
    assert db_path.is_file() and db_path.stat().st_size > 0
    # Atomic publish leaves no temporary siblings behind.
    assert not [p for p in runtime.iterdir() if ".tmp" in p.name]
    marker = worker_tools._read_candidate_db_marker(db_path)
    assert marker == {
        "packet_sha256": packet["packet_sha256"],
        "target_request_id": "target-request-0",
        "target_task_id": "TARGET_TASK_0",
    }
    # A second prewarm of the same verified packet reuses the overlay.
    again = worker_tools.prewarm_quality_review_source_graph(
        packet_path, repo=candidate, authority_repo=canonical
    )
    assert again["ok"] is True
    assert again["built"] is False


def test_quality_review_source_graph_query_is_query_only_and_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    canonical, candidate, runtime, packet = _write_candidate(tmp_path)
    packet_path = runtime / "quality_review_packet.json"
    ctx = _ctx(
        runtime,
        repo=candidate,
        authority_repo=canonical,
        packet_path=packet_path,
        task_id="REVIEW_TASK_0",
    )

    build_calls: list[object] = []
    real_build_index = source_graph.build_index

    def fake_build_index(*args: object, **kwargs: object) -> object:
        # The full suite intentionally runs background indexing probes. This
        # guard owns only the candidate overlay under test; an unrelated
        # repository finishing concurrently is not a query-time build here.
        if not args or Path(args[0]).resolve() != candidate.resolve():
            return real_build_index(*args, **kwargs)
        build_calls.append((args, kwargs))
        raise AssertionError("build_index must never run at query time")

    monkeypatch.setattr(source_graph, "build_index", fake_build_index)
    worker_tools._CACHE.clear()

    # Without a prewarmed overlay the query fails closed and never builds.
    result = worker_tools.source_graph_query(
        ctx, mode="function", query="candidate_only_symbol_0", budget=16
    )
    assert result["ok"] is False
    assert "quality_review_candidate_source_graph_unavailable" in str(
        result.get("reason") or ""
    )
    assert build_calls == []

    # Prewarm with the real build path, then re-arm the query-time guard.
    monkeypatch.setattr(source_graph, "build_index", real_build_index)
    prewarm = worker_tools.prewarm_quality_review_source_graph(
        packet_path, repo=candidate, authority_repo=canonical
    )
    assert prewarm["ok"] is True
    monkeypatch.setattr(source_graph, "build_index", fake_build_index)
    worker_tools._CACHE.clear()

    result = worker_tools.source_graph_query(
        ctx, mode="function", query="candidate_only_symbol_0", budget=16
    )
    assert result["ok"] is True
    assert result["authority_source"] == "candidate_overlay"
    assert "candidate_only_symbol_0" in result["content"]
    assert build_calls == []


def test_quality_review_corrupt_or_empty_overlay_fails_closed(
    tmp_path: Path,
) -> None:
    canonical, candidate, runtime, packet = _write_candidate(tmp_path)
    packet_path = runtime / "quality_review_packet.json"
    prewarm = worker_tools.prewarm_quality_review_source_graph(
        packet_path, repo=candidate, authority_repo=canonical
    )
    db_path = Path(prewarm["db_path"])
    # Corrupt the overlay after prewarm; runtime must fail closed, not rebuild.
    db_path.write_bytes(b"")
    ctx = _ctx(
        runtime,
        repo=candidate,
        authority_repo=canonical,
        packet_path=packet_path,
        task_id="REVIEW_TASK_0",
    )
    worker_tools._CACHE.clear()
    result = worker_tools.source_graph_query(
        ctx, mode="function", query="candidate_only_symbol_0", budget=16
    )
    assert result["ok"] is False
    assert "quality_review_candidate_source_graph_unavailable" in str(
        result.get("reason") or ""
    )


def test_three_reviewer_overlays_plus_coordinator_query_no_leakage_or_starvation(
    tmp_path: Path, monkeypatch
) -> None:
    overlays = []
    for index in range(3):
        canonical, candidate, runtime, packet = _write_candidate(tmp_path, index=index)
        packet_path = runtime / "quality_review_packet.json"
        prewarm = worker_tools.prewarm_quality_review_source_graph(
            packet_path, repo=candidate, authority_repo=canonical
        )
        assert prewarm["ok"] is True
        ctx = _ctx(
            runtime,
            repo=candidate,
            authority_repo=canonical,
            packet_path=packet_path,
            task_id=f"REVIEW_TASK_{index}",
        )
        overlays.append((ctx, f"candidate_only_symbol_{index}"))

    # A lightweight coordinator query against a real canonical database.
    coordinator_repo = tmp_path / "coordinator"
    coordinator_repo.mkdir()
    (coordinator_repo / "coordinator.py").write_text(
        "def coordinator_symbol():\n    return 99\n", encoding="utf-8"
    )
    coordinator_db = tmp_path / "coordinator.sqlite"
    with source_graph.database_path_override(coordinator_db):
        source_graph.build_index(coordinator_repo, db_path=coordinator_db)
    coordinator_binding = worker_tools.AuthorityBinding(
        db_path=coordinator_db,
        authority_source="canonical",
        authority_state="sole_authority",
        authority_repo=coordinator_repo,
    )
    monkeypatch.setattr(
        worker_tools,
        "_canonical_source_graph_binding",
        lambda _ctx: coordinator_binding,
    )
    coordinator_ctx = _ctx(
        tmp_path / "coordinator_runtime",
        repo=coordinator_repo,
        authority_repo=coordinator_repo,
        packet_path=None,
        task_id="COORD_TASK",
    )
    worker_tools._CACHE.clear()

    def query_overlay(ctx, symbol):
        return worker_tools.source_graph_query(
            ctx, mode="function", query=symbol, budget=16
        )

    def query_coordinator(ctx):
        return worker_tools.source_graph_query(
            ctx, mode="function", query="coordinator_symbol", budget=16
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(query_overlay, ctx, symbol) for ctx, symbol in overlays]
        futures.append(pool.submit(query_coordinator, coordinator_ctx))
        results = [future.result(timeout=60) for future in futures]

    for (ctx, symbol), result in zip(overlays, results[:3]):
        assert result["ok"] is True
        assert result["authority_source"] == "candidate_overlay"
        assert symbol in result["content"]
        # No cross-overlay leakage: each overlay only sees its own symbol.
        for _, other_symbol in overlays:
            if other_symbol != symbol:
                assert other_symbol not in result["content"]

    coordinator_result = results[3]
    assert coordinator_result["ok"] is True
    assert coordinator_result["authority_source"] == "canonical"
    assert "coordinator_symbol" in coordinator_result["content"]


def _mutate_overlay(db_path: Path, sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _prewarmed_reviewer_query(tmp_path: Path, *, index: int = 0):
    canonical, candidate, runtime, packet = _write_candidate(tmp_path, index=index)
    packet_path = runtime / "quality_review_packet.json"
    prewarm = worker_tools.prewarm_quality_review_source_graph(
        packet_path, repo=candidate, authority_repo=canonical
    )
    ctx = _ctx(
        runtime,
        repo=candidate,
        authority_repo=canonical,
        packet_path=packet_path,
        task_id=f"REVIEW_TASK_{index}",
    )
    return Path(prewarm["db_path"]), ctx, f"candidate_only_symbol_{index}"


def test_quality_review_overlay_missing_changed_path_row_fails_closed(
    tmp_path: Path,
) -> None:
    db_path, ctx, symbol = _prewarmed_reviewer_query(tmp_path)
    # Correct marker, but the packet changed-path row is gone from ``files``.
    _mutate_overlay(db_path, "DELETE FROM files WHERE file_path=?", ("module.py",))
    worker_tools._CACHE.clear()
    result = worker_tools.source_graph_query(
        ctx, mode="function", query=symbol, budget=16
    )
    assert result["ok"] is False
    assert "quality_review_candidate_source_graph_unavailable" in str(
        result.get("reason") or ""
    )


def test_quality_review_overlay_wrong_source_hash_fails_closed(
    tmp_path: Path,
) -> None:
    db_path, ctx, symbol = _prewarmed_reviewer_query(tmp_path)
    # Correct marker, but the indexed source_hash no longer matches the bytes.
    _mutate_overlay(
        db_path,
        "UPDATE files SET source_hash=? WHERE file_path=?",
        ("0" * 64, "module.py"),
    )
    worker_tools._CACHE.clear()
    result = worker_tools.source_graph_query(
        ctx, mode="function", query=symbol, budget=16
    )
    assert result["ok"] is False
    assert "quality_review_candidate_source_graph_unavailable" in str(
        result.get("reason") or ""
    )


def test_quality_review_overlay_correct_marker_but_partial_schema_fails_closed(
    tmp_path: Path,
) -> None:
    db_path, ctx, symbol = _prewarmed_reviewer_query(tmp_path)
    # Marker survives, but a required table is dropped -> partial schema.
    _mutate_overlay(db_path, "DROP TABLE entities")
    worker_tools._CACHE.clear()
    result = worker_tools.source_graph_query(
        ctx, mode="function", query=symbol, budget=16
    )
    assert result["ok"] is False
    assert "quality_review_candidate_source_graph_unavailable" in str(
        result.get("reason") or ""
    )


def test_quality_review_prewarm_fails_before_publish_on_wrong_hash(
    tmp_path: Path, monkeypatch
) -> None:
    canonical, candidate, runtime, packet = _write_candidate(tmp_path)
    packet_path = runtime / "quality_review_packet.json"
    real_index_file = source_graph.index_file

    def fake_index_file(repo: Path, path: str, expected_hash: str) -> dict:
        # Reconcile the changed path, then corrupt its hash in the private
        # context-bound clone.  Temp verification must fail closed before the
        # clone is atomically published.
        result = real_index_file(repo, path, expected_hash)
        conn = sqlite3.connect(str(source_graph.resolve_db_path(repo)))
        try:
            conn.execute(
                "UPDATE files SET source_hash=? WHERE file_path=?",
                ("0" * 64, "module.py"),
            )
            conn.commit()
        finally:
            conn.close()
        return result

    monkeypatch.setattr(source_graph, "index_file", fake_index_file)
    with pytest.raises(
        worker_tools.WorkerToolError,
        match="quality_review_candidate_source_graph_empty",
    ):
        worker_tools.prewarm_quality_review_source_graph(
            packet_path, repo=candidate, authority_repo=canonical
        )
    assert not list(runtime.glob("candidate_source_graph_*.sqlite"))


def test_registered_quality_review_schema_exposes_canonical_finding_shape(
    tmp_path: Path,
) -> None:
    """The generated quality_review_submit schema consumes the one typed model."""
    repo = tmp_path / "repo"
    authority = tmp_path / "authority"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    authority.mkdir()
    ctx = _ctx(
        runtime,
        repo=repo,
        authority_repo=authority,
        packet_path=None,
        task_id="SCHEMA_TASK",
    )
    server = worker_tools.build_server(ctx)

    if hasattr(server, "_tools"):  # stdlib fallback without mcp installed
        function = server._tools["aiworkhub_worker_quality_review_submit"]
        model = get_args(get_type_hints(function)["findings"])[0]
        assert model is quality_reviewer.QualityReviewFinding
        assert frozenset(model.__annotations__) == (
            quality_reviewer.QUALITY_REVIEW_FINDING_INPUT_KEYS
        )
        assert frozenset(model.__required_keys__) == (
            quality_reviewer.QUALITY_REVIEW_FINDING_INPUT_REQUIRED_KEYS
        )
        return

    for tool in asyncio.run(server.list_tools()):
        if tool.name != "aiworkhub_worker_quality_review_submit":
            continue
        schema = getattr(tool, "inputSchema", None)
        if schema is None:
            schema = getattr(tool, "parameters", None).model_json_schema()
        items = schema["properties"]["findings"]["items"]
        if "$ref" in items:
            items = schema["$defs"][items["$ref"].rsplit("/", 1)[-1]]
        assert frozenset(items.get("required", ())) == (
            quality_reviewer.QUALITY_REVIEW_FINDING_INPUT_REQUIRED_KEYS
        )
        assert frozenset(items.get("properties", {})) == (
            quality_reviewer.QUALITY_REVIEW_FINDING_INPUT_KEYS
        )
        return
    pytest.fail("aiworkhub_worker_quality_review_submit not registered")
