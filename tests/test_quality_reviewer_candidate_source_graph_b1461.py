from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import process_launcher
from aiworkhub import quality_reviewer
from aiworkhub import repository_state
from aiworkhub import source_graph
from aiworkhub import storage_registry
from aiworkhub import worker_ai_tools_mcp as worker_tools


def _ctx(
    runtime: Path,
    *,
    repo: Path,
    authority_repo: Path,
    packet_path: Path | None,
) -> worker_tools.WorkerToolContext:
    runtime.mkdir(parents=True, exist_ok=True)
    ledger = runtime / "audit.jsonl"
    # Created directly at 0600 (never via write_text + chmod): the worker
    # sandbox blocks fchmod/chmod, and _append_line_0600 only tolerates that
    # denial when the file's mode is already exactly 0600, mirroring how the
    # coordinator pre-creates the real request-private ledger before launch.
    ledger_fd = os.open(ledger, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.close(ledger_fd)
    key = runtime / "audit.key"
    key.write_bytes(b"k" * 32)
    return worker_tools.WorkerToolContext(
        task_id="REVIEW_TASK_1" if packet_path else "WORKER_TASK_1",
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


def _bootstrap_canonical_repo(repo: Path) -> None:
    """Real repo identity + canonical-only storage registry + a real built
    Source Graph generation.  This exercises the genuine
    ``resolve_db_path``/``load_storage_registry`` path instead of
    monkeypatching authority resolution away -- write any source files under
    ``repo`` before calling this.
    """

    repository_state.bootstrap_repository(repo)
    source_graph.build_index(repo, incremental=False)


def test_ordinary_worker_source_graph_remains_canonical(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "worker"
    authority = tmp_path / "canonical"
    repo.mkdir()
    authority.mkdir()
    (authority / "module.py").write_text(
        "def canonical_marker():\n    return 1\n", encoding="utf-8"
    )
    _bootstrap_canonical_repo(authority)
    ctx = _ctx(tmp_path / "runtime", repo=repo, authority_repo=authority, packet_path=None)
    observed: list[Path] = []
    monkeypatch.setattr(
        source_graph,
        "focus",
        lambda root, query, budget: observed.append(root) or {"matches": [{"name": query}]},
    )
    worker_tools._CACHE.clear()

    result = worker_tools.source_graph_query(ctx, mode="focus", query="authority", budget=8)

    assert result["ok"] is True
    assert result["authority_source"] == "canonical"
    assert Path(result["authority_repo"]).resolve() == authority.resolve()
    assert [p.resolve() for p in observed] == [authority.resolve()]


def test_canonical_source_graph_authority_resolves_to_exact_registry_bound_db(
    tmp_path: Path,
) -> None:
    """authority_repo/storage registry must resolve to the exact bound DB,
    and only a verified canonical ``authority_source``+``sole_authority``
    ``authority_state`` binding is ever returned.
    """

    authority = tmp_path / "canonical"
    authority.mkdir()
    (authority / "module.py").write_text(
        "def canonical_marker():\n    return 1\n", encoding="utf-8"
    )
    _bootstrap_canonical_repo(authority)

    binding = worker_tools.verify_quality_review_prewarm_authority(authority)

    assert binding.authority_source == "canonical"
    assert binding.authority_state == "sole_authority"
    registry = storage_registry.load_storage_registry(authority.resolve())
    expected = storage_registry.resolve_database_path(registry, "source_graph")
    assert binding.db_path == expected
    assert binding.db_path.is_file()


@pytest.mark.parametrize(
    "corrupt, match",
    [
        ("shadow_state", "not_canonical_active"),
        ("missing_db", "absent_or_empty"),
        ("symlinked_db", "symlink"),
        ("schema_incomplete", "schema_incomplete"),
        ("wrong_revision", "wrong_revision"),
        ("forged_registry_repo_id", "authority_registry_unresolved"),
    ],
)
def test_canonical_source_graph_authority_fails_closed(
    tmp_path: Path, corrupt: str, match: str
) -> None:
    authority = tmp_path / f"canonical-{corrupt}"
    authority.mkdir()
    (authority / "module.py").write_text(
        "def canonical_marker():\n    return 1\n", encoding="utf-8"
    )
    _bootstrap_canonical_repo(authority)
    registry = storage_registry.load_storage_registry(authority.resolve())
    db_path = storage_registry.resolve_database_path(registry, "source_graph")
    registry_path = authority / storage_registry.STORAGE_REGISTRY_REL

    if corrupt == "shadow_state":
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
        for entry in payload["databases"]:
            if entry["id"] == "source_graph":
                entry["authority"] = {
                    "state": "shadow",
                    "canonical_active": False,
                    "legacy_active": False,
                    "live_cutover": False,
                }
        registry_path.write_text(json.dumps(payload), encoding="utf-8")
    elif corrupt == "missing_db":
        db_path.unlink()
    elif corrupt == "symlinked_db":
        if os.name == "nt":
            pytest.skip("symlinks unavailable on Windows")
        real = tmp_path / "elsewhere.sqlite"
        db_path.replace(real)
        os.symlink(real, db_path)
    elif corrupt == "schema_incomplete":
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DROP TABLE entities_fts")
            conn.commit()
        finally:
            conn.close()
    elif corrupt == "wrong_revision":
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT key, value FROM meta WHERE key='last_build'"
            ).fetchall()
            for key, value in rows:
                payload = json.loads(value)
                payload["build_revision"] = "aiworkhub.source_graph.forged.v0"
                conn.execute(
                    "UPDATE meta SET value=? WHERE key=?",
                    (json.dumps(payload), key),
                )
            conn.commit()
        finally:
            conn.close()
    elif corrupt == "forged_registry_repo_id":
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
        payload["repo_id"] = "repo_" + "f" * 32
        registry_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(worker_tools.WorkerToolError, match=match):
        worker_tools.verify_quality_review_prewarm_authority(authority)


def test_quality_reviewer_source_graph_uses_packet_bound_candidate_overlay(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    (canonical / "module.py").write_text("def canonical_only():\n    return 1\n", encoding="utf-8")
    _bootstrap_canonical_repo(canonical)
    candidate_file = candidate / "module.py"
    candidate_file.write_text(
        "def canonical_only():\n    return 1\n\n"
        "def candidate_only_symbol():\n    return 2\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    packet = quality_reviewer.build_review_packet(
        request_id="target-request-1",
        task_id="TARGET_TASK_1",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes={
            "module.py": hashlib.sha256(candidate_file.read_bytes()).hexdigest()
        },
    )
    packet_path = runtime / "quality_review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    prewarm = worker_tools.prewarm_quality_review_source_graph(
        packet_path, repo=candidate, authority_repo=canonical,
    )
    assert prewarm["ok"] is True
    assert prewarm["built"] is True
    ctx = _ctx(runtime, repo=candidate, authority_repo=canonical, packet_path=packet_path)
    worker_tools._CACHE.clear()

    result = worker_tools.source_graph_query(
        ctx,
        mode="function",
        query="candidate_only_symbol",
        budget=16,
    )

    assert result["ok"] is True
    assert result["hit_count"] > 0
    assert "candidate_only_symbol" in result["content"]
    assert result["authority_source"] == "candidate_overlay"
    assert result["authority_state"] == "quality_review_readonly"
    assert result["authority_repo"] == str(candidate.resolve())
    assert result["target_request_id"] == "target-request-1"
    assert result["target_task_id"] == "TARGET_TASK_1"
    assert result["packet_sha256"] == packet["packet_sha256"]

    audit = worker_tools.verify_audit_ledger(
        ctx.audit_ledger_path,
        ctx.audit_hmac_key_path,
        task_id=ctx.task_id,
        runner=ctx.runner,
        topic=ctx.topic,
        request_id=ctx.request_id,
    )
    assert audit["live_source_graph_calls"] == 1
    assert audit["authority_index_identity"] == [
        f"source_graph:candidate_overlay:quality_review_readonly:{candidate.resolve()}"
    ]


def _exists_ready(db_path: Path, *args: object, **kwargs: object) -> bool:
    try:
        return db_path.is_file() and db_path.stat().st_size > 0
    except OSError:
        return False


def _make_candidate_packet(runtime: Path, candidate: Path, *, index: int) -> Path:
    runtime.mkdir()
    candidate.mkdir()
    candidate_file = candidate / "module.py"
    candidate_file.write_text(
        "def canonical_only():\n    return 1\n\n"
        f"def candidate_only_symbol_{index}():\n    return {index}\n",
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
    return packet_path


def test_quality_review_prewarm_distinct_candidates_build_concurrently_without_build_index(
    tmp_path: Path, monkeypatch
) -> None:
    """Three concurrent lenses in one compact test: (1) zero
    ``source_graph.build_index`` calls, (2) distinct packet DBs built
    concurrently, serviced independently of each other (a stuck packet can
    never block an unrelated one, since single-flight keys on ``db_path`` and
    the per-packet writer lease keys on each packet's own runtime directory),
    (3) candidate isolation -- one packet's symbol never leaks into another
    packet's overlay.  Plus bounded completion (joined with a timeout) and
    no leftover ``.tmp`` residue once every thread finishes.  Each packet
    gets its own runtime directory, exactly as distinct concurrent reviewer
    requests do in production (each owns a private
    ``task_mcp_worker_runtime`` directory) -- sharing one runtime directory
    across packets would collide on the single-writer index lease, which is
    scoped to the runtime directory, not the individual db file.

    A fourth thread runs a real bounded canonical coordinator Source Graph
    query (``source_graph.focus``) against the same ``canonical`` repo
    concurrently with the three prewarms.  It must complete quickly and
    successfully: the read-only canonical connection each prewarm opens for
    its SQLite backup must never starve, or be starved by, an ordinary
    coordinator read against that same canonical database.
    """

    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "module.py").write_text("def canonical_only():\n    return 1\n", encoding="utf-8")
    _bootstrap_canonical_repo(canonical)

    def fail_build(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("prewarm must never call build_index")

    monkeypatch.setattr(source_graph, "build_index", fail_build)

    packet_paths = []
    candidates = []
    for index in range(3):
        runtime = tmp_path / f"runtime-{index}"
        candidate = tmp_path / f"candidate-{index}"
        candidates.append(candidate)
        packet_paths.append(_make_candidate_packet(runtime, candidate, index=index))

    results: list[dict] = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()

    def run(index: int) -> None:
        try:
            result = worker_tools.prewarm_quality_review_source_graph(
                packet_paths[index], repo=candidates[index], authority_repo=canonical,
            )
            with results_lock:
                results.append(result)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    query_results: list[dict] = []
    query_errors: list[BaseException] = []

    def run_coordinator_query() -> None:
        try:
            query_results.append(source_graph.focus(canonical, "canonical_only", 8))
        except BaseException as exc:  # noqa: BLE001
            query_errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(3)]
    query_thread = threading.Thread(target=run_coordinator_query)
    for thread in [*threads, query_thread]:
        thread.start()

    query_join_started = time.monotonic()
    query_thread.join(timeout=10)
    query_join_duration = time.monotonic() - query_join_started
    assert not query_thread.is_alive(), "coordinator query starved by concurrent prewarms"
    assert query_join_duration < 10, f"coordinator query starved: took {query_join_duration:.2f}s"
    assert not query_errors, query_errors
    assert len(query_results) == 1
    assert len(query_results[0]["matches"]) >= 1, "coordinator query found no canonical matches"

    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "prewarm did not complete"

    assert not errors, errors
    assert len(results) == 3
    assert all(r["built"] for r in results)
    db_paths = {Path(r["db_path"]) for r in results}
    assert len(db_paths) == 3, "each packet must build its own distinct db"
    assert all(path.is_file() for path in db_paths)
    residue = [str(f) for path in db_paths for f in path.parent.glob(".*.tmp")]
    assert not residue, f"leftover temp file residue: {residue}"

    by_task_id = {result["target_task_id"]: result for result in results}
    for index in range(3):
        result = by_task_id[f"TARGET_TASK_{index}"]
        db_path = Path(result["db_path"]).resolve()
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        try:
            qualnames = {
                str(row[0]) for row in conn.execute("SELECT qualname FROM entities")
            }
        finally:
            conn.close()
        own_symbol = f"candidate_only_symbol_{index}"
        assert any(own_symbol in qualname for qualname in qualnames)
        for other in range(3):
            if other == index:
                continue
            other_symbol = f"candidate_only_symbol_{other}"
            assert not any(other_symbol in qualname for qualname in qualnames), (
                f"candidate {index} leaked candidate {other}'s symbol"
            )


def test_quality_review_prewarm_same_packet_single_flights(
    tmp_path: Path, monkeypatch
) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "module.py").write_text("def canonical_only():\n    return 1\n", encoding="utf-8")
    _bootstrap_canonical_repo(canonical)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    candidate_file = candidate / "module.py"
    candidate_file.write_text(
        "def canonical_only():\n    return 1\n\n"
        "def candidate_only_symbol():\n    return 2\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    packet = quality_reviewer.build_review_packet(
        request_id="target-request-1",
        task_id="TARGET_TASK_1",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes={
            "module.py": hashlib.sha256(candidate_file.read_bytes()).hexdigest()
        },
    )
    packet_path = runtime / "quality_review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")

    def fail_build(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("prewarm must never call build_index")

    monkeypatch.setattr(source_graph, "build_index", fail_build)

    real_index_file = source_graph.index_file
    call_count = {"n": 0}
    call_lock = threading.Lock()
    release = threading.Event()

    def fake_index_file(repo_root: Path, path: str, expected_hash: str):
        with call_lock:
            call_count["n"] += 1
        release.wait(timeout=10)
        return real_index_file(repo_root, path, expected_hash)

    monkeypatch.setattr(source_graph, "index_file", fake_index_file)

    results: list[dict] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(
                worker_tools.prewarm_quality_review_source_graph(
                    packet_path, repo=candidate, authority_repo=canonical,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    first = threading.Thread(target=run)
    first.start()
    deadline = time.monotonic() + 5
    while call_count["n"] == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert call_count["n"] == 1
    second = threading.Thread(target=run)
    second.start()
    release.set()
    first.join(timeout=15)
    second.join(timeout=15)

    assert not errors, errors
    assert call_count["n"] == 1
    assert sorted(r["built"] for r in results) == [False, True]


def test_quality_reviewer_query_fails_closed_without_prewarm(
    tmp_path: Path, monkeypatch
) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    (canonical / "module.py").write_text(
        "def canonical_only():\n    return 1\n", encoding="utf-8"
    )
    candidate_file = candidate / "module.py"
    candidate_file.write_text(
        "def canonical_only():\n    return 1\n\n"
        "def candidate_only_symbol():\n    return 2\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    packet = quality_reviewer.build_review_packet(
        request_id="target-request-1",
        task_id="TARGET_TASK_1",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes={
            "module.py": hashlib.sha256(candidate_file.read_bytes()).hexdigest()
        },
    )
    packet_path = runtime / "quality_review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _ctx(
        runtime, repo=candidate, authority_repo=canonical, packet_path=packet_path
    )
    worker_tools._CACHE.clear()

    build_calls: list[Path] = []

    def fail_build(_repo, *, db_path, incremental):
        build_calls.append(db_path)
        raise AssertionError("runtime reviewer query must never build lazily")

    monkeypatch.setattr(source_graph, "build_index", fail_build)

    result = worker_tools.source_graph_query(
        ctx, mode="focus", query="candidate_only_symbol", budget=8,
    )

    assert result["ok"] is False
    assert "unavailable" in result["reason"]
    assert build_calls == []


def test_review_packet_source_evidence_centers_nf3_late_changed_symbol(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    prefix = "# unchanged filler\n" * 260
    assert len(prefix.encode("utf-8")) > 4_000
    original = prefix + "def unchanged_tail():\n    return None\n"
    changed = (
        prefix
        + "def _v3_planned_outputs():\n"
        + "    return ['tests/test_quality_reviewer_candidate_source_graph_b1461.py']\n"
    )
    (canonical / "module.py").write_text(original, encoding="utf-8")
    candidate_file = candidate / "module.py"
    candidate_file.write_text(changed, encoding="utf-8")
    digest = hashlib.sha256(candidate_file.read_bytes()).hexdigest()
    manager = SimpleNamespace(
        repo=canonical,
        _QUALITY_REVIEW_SOURCE_TOTAL_MAX_BYTES=60_000,
        _QUALITY_REVIEW_SOURCE_MAX_BYTES=4_000,
        _QUALITY_REVIEW_SOURCE_CONTEXT_LINES=3,
    )
    workspace = SimpleNamespace(path=candidate)

    evidence = process_launcher.ProcessManager._quality_review_source_evidence(
        manager, workspace, {"module.py": digest}
    )
    packet = quality_reviewer.build_review_packet(
        request_id="target-request-1",
        task_id="TARGET_TASK_1",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes={"module.py": digest},
        source_evidence=evidence,
    )
    row = packet["candidate"]["source_evidence"][0]

    filler_count = row["excerpt"].count("# unchanged filler")
    assert 0 < filler_count <= manager._QUALITY_REVIEW_SOURCE_CONTEXT_LINES
    assert "_v3_planned_outputs" in row["excerpt"][:200]
    assert (
        "tests/test_quality_reviewer_candidate_source_graph_b1461.py" in row["excerpt"]
    )
    assert row["segments"][0]["candidate_start_line"] > 250
    assert row["candidate_sha256"] == digest


def test_review_packet_many_nf3_hunks_stays_per_path_bounded(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    baseline_lines: list[str] = []
    candidate_lines: list[str] = []
    changed_hunks = 80
    for hunk_index in range(changed_hunks):
        baseline_lines.append(f"def unchanged_block_{hunk_index}():\n")
        baseline_lines.append(f"    return {hunk_index}\n")
        candidate_lines.append(f"def unchanged_block_{hunk_index}():\n")
        candidate_lines.append(f"    return {hunk_index}\n")
        baseline_lines.append(f"NF3_VALUE_{hunk_index} = 'old'\n")
        candidate_lines.append(
            f"NF3_VALUE_{hunk_index} = '_v3_planned_outputs_{hunk_index}'\n"
        )
        for filler_index in range(8):
            filler = f"# stable spacer {hunk_index}:{filler_index}\n"
            baseline_lines.append(filler)
            candidate_lines.append(filler)
    (canonical / "module.py").write_text("".join(baseline_lines), encoding="utf-8")
    candidate_file = candidate / "module.py"
    candidate_file.write_text("".join(candidate_lines), encoding="utf-8")
    digest = hashlib.sha256(candidate_file.read_bytes()).hexdigest()
    manager = SimpleNamespace(
        repo=canonical,
        _QUALITY_REVIEW_SOURCE_TOTAL_MAX_BYTES=60_000,
        _QUALITY_REVIEW_SOURCE_MAX_BYTES=4_000,
        _QUALITY_REVIEW_SOURCE_CONTEXT_LINES=3,
    )
    workspace = SimpleNamespace(path=candidate)

    evidence = process_launcher.ProcessManager._quality_review_source_evidence(
        manager, workspace, {"module.py": digest}
    )
    packet = quality_reviewer.build_review_packet(
        request_id="target-request-1",
        task_id="TARGET_TASK_1",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes={"module.py": digest},
        source_evidence=evidence,
    )
    row = packet["candidate"]["source_evidence"][0]
    omitted = int(row["omission_reason"].removeprefix("changed_hunks_omitted:"))

    assert packet["packet_sha256"]
    assert row["candidate_sha256"] == digest
    assert row["excerpt_bytes"] <= manager._QUALITY_REVIEW_SOURCE_MAX_BYTES
    assert (
        len(row["excerpt"].encode("utf-8"))
        <= manager._QUALITY_REVIEW_SOURCE_MAX_BYTES
    )
    assert sum(segment["excerpt_bytes"] for segment in row["segments"]) == row[
        "excerpt_bytes"
    ]
    assert len(row["segments"]) == changed_hunks
    assert all(segment["changed_start_line"] > 0 for segment in row["segments"])
    assert all(segment["baseline_start_line"] > 0 for segment in row["segments"])
    assert row["segments"][-1]["truncated"] is True
    assert omitted > 0


def test_review_packet_rejects_omitted_hunks_without_exact_range_metadata() -> None:
    digest = hashlib.sha256(b"VALUE = 'new'\n").hexdigest()

    with pytest.raises(
        quality_reviewer.ReviewerEvidenceError,
        match="invalid_candidate_source_evidence",
    ):
        quality_reviewer.build_review_packet(
            request_id="target-request-1",
            task_id="TARGET_TASK_1",
            claim_epoch=1,
            worker_provider="codex_cli",
            changed_path_hashes={"module.py": digest},
            source_evidence={
                "module.py": {
                    "candidate_sha256": digest,
                    "excerpt": "",
                    "excerpt_bytes": 0,
                    "source_bytes": 14,
                    "truncated": True,
                    "segments": [],
                    "omission_reason": "changed_hunks_omitted:1",
                }
            },
        )


@pytest.mark.skipif(os.name == "nt", reason="Windows forbids control characters in paths")
def test_review_packet_escapes_control_character_path_in_hunk_header(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    raw_path = "dir/weird\n@@ injected\x1f.py"
    baseline_file = canonical / raw_path
    candidate_file = candidate / raw_path
    baseline_file.parent.mkdir(parents=True)
    candidate_file.parent.mkdir(parents=True)
    baseline_file.write_text("VALUE = 'old'\n", encoding="utf-8")
    candidate_file.write_text("VALUE = '_v3_planned_outputs'\n", encoding="utf-8")
    digest = hashlib.sha256(candidate_file.read_bytes()).hexdigest()
    manager = SimpleNamespace(
        repo=canonical,
        _QUALITY_REVIEW_SOURCE_TOTAL_MAX_BYTES=60_000,
        _QUALITY_REVIEW_SOURCE_MAX_BYTES=4_000,
        _QUALITY_REVIEW_SOURCE_CONTEXT_LINES=3,
    )
    workspace = SimpleNamespace(path=candidate)

    evidence = process_launcher.ProcessManager._quality_review_source_evidence(
        manager, workspace, {raw_path: digest}
    )
    packet = quality_reviewer.build_review_packet(
        request_id="target-request-1",
        task_id="TARGET_TASK_1",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes={raw_path: digest},
        source_evidence=evidence,
    )
    row = packet["candidate"]["source_evidence"][0]

    assert row["path"] == raw_path
    assert row["candidate_sha256"] == digest
    assert 'path:"dir/weird\\n@@ injected\\u001f.py"' in row["excerpt"]
    assert raw_path not in row["excerpt"]
    assert "\n@@ injected" not in row["excerpt"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks unavailable on Windows")
def test_review_packet_rejects_inside_repo_symlink_candidate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "real.py"
    target.write_text("VALUE = 'real'\n", encoding="utf-8")
    # A symlink whose target resolves INSIDE the repo must still be rejected:
    # resolve()-then-is_symlink() would follow it and never see the link.
    os.symlink(target, repo / "link.py")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    packet = quality_reviewer.build_review_packet(
        request_id="target-request-1",
        task_id="TARGET_TASK_1",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes={"link.py": digest},
    )
    packet_path = tmp_path / "quality_review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")

    with pytest.raises(
        quality_reviewer.ReviewerEvidenceError,
        match="quality_review_candidate_path_symlink",
    ):
        quality_reviewer.verify_review_packet_candidate(packet_path, repo)


@pytest.mark.skipif(os.name == "nt", reason="symlinks unavailable on Windows")
def test_review_packet_rejects_outside_repo_symlink_candidate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 'outside'\n", encoding="utf-8")
    os.symlink(outside, repo / "link.py")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    packet = quality_reviewer.build_review_packet(
        request_id="target-request-1",
        task_id="TARGET_TASK_1",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes={"link.py": digest},
    )
    packet_path = tmp_path / "quality_review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")

    with pytest.raises(
        quality_reviewer.ReviewerEvidenceError,
        match="quality_review_candidate_path_symlink",
    ):
        quality_reviewer.verify_review_packet_candidate(packet_path, repo)


def test_quality_review_prewarm_skips_non_indexable_changed_path_without_aborting(
    tmp_path: Path,
) -> None:
    """A packet touching both an indexable and a non-indexable path must
    still prewarm successfully: Source Graph has no representation for the
    non-indexable extension, so it is skipped rather than aborting the whole
    build, while the indexable path remains exact hash-bound and queryable.
    """

    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    (canonical / "module.py").write_text("def canonical_only():\n    return 1\n", encoding="utf-8")
    _bootstrap_canonical_repo(canonical)
    candidate_module = candidate / "module.py"
    candidate_module.write_text(
        "def canonical_only():\n    return 1\n\n"
        "def candidate_only_symbol():\n    return 2\n",
        encoding="utf-8",
    )
    candidate_notes = candidate / "NOTES.txt"
    candidate_notes.write_text("not source code\n", encoding="utf-8")
    candidate_data = candidate / "data.csv"
    candidate_data.write_text("a,b\n1,2\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    packet = quality_reviewer.build_review_packet(
        request_id="target-request-1",
        task_id="TARGET_TASK_1",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes={
            "module.py": hashlib.sha256(candidate_module.read_bytes()).hexdigest(),
            "NOTES.txt": hashlib.sha256(candidate_notes.read_bytes()).hexdigest(),
            "data.csv": hashlib.sha256(candidate_data.read_bytes()).hexdigest(),
        },
    )
    packet_path = runtime / "quality_review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")

    prewarm = worker_tools.prewarm_quality_review_source_graph(
        packet_path, repo=candidate, authority_repo=canonical,
    )

    assert prewarm["ok"] is True
    assert prewarm["built"] is True

    db_path = Path(prewarm["db_path"]).resolve()
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        file_paths = {str(row[0]) for row in conn.execute("SELECT file_path FROM files")}
    finally:
        conn.close()
    assert "NOTES.txt" not in file_paths
    assert "data.csv" not in file_paths
    assert "module.py" in file_paths

    ctx = _ctx(runtime, repo=candidate, authority_repo=canonical, packet_path=packet_path)
    worker_tools._CACHE.clear()

    result = worker_tools.source_graph_query(
        ctx, mode="function", query="candidate_only_symbol", budget=16,
    )
    assert result["ok"] is True
    assert result["hit_count"] > 0
    assert "candidate_only_symbol" in result["content"]


def test_quality_review_prewarm_all_concurrent_callers_observe_wrapped_data_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    """A Source Graph contract/data failure raised while reconciling an
    indexable changed path (for example a hash-mismatch/corruption
    ``SourceGraphError``) must never leak past ``worker_ai_tools_mcp``'s own
    boundary: both the single-flight owner and any concurrent joiner must
    observe the same normalized ``WorkerToolError``, never the raw
    ``source_graph.SourceGraphError`` -- this is what lets
    ``ProcessManager`` classify the failure truthfully as
    ``quality_review_source_graph_prewarm_failed`` instead of an unexpected
    provider-launch error.
    """

    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    (canonical / "module.py").write_text("def canonical_only():\n    return 1\n", encoding="utf-8")
    _bootstrap_canonical_repo(canonical)
    candidate_module = candidate / "module.py"
    candidate_module.write_text(
        "def canonical_only():\n    return 1\n\n"
        "def candidate_only_symbol():\n    return 2\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    packet = quality_reviewer.build_review_packet(
        request_id="target-request-1",
        task_id="TARGET_TASK_1",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes={
            "module.py": hashlib.sha256(candidate_module.read_bytes()).hexdigest()
        },
    )
    packet_path = runtime / "quality_review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")

    def fail_index_file(*_args: object, **_kwargs: object) -> None:
        raise source_graph.SourceGraphError("source_graph_single_file_hash_mismatch:boom")

    monkeypatch.setattr(source_graph, "index_file", fail_index_file)

    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    start = threading.Barrier(2)

    def run() -> None:
        start.wait(timeout=5)
        try:
            worker_tools.prewarm_quality_review_source_graph(
                packet_path, repo=candidate, authority_repo=canonical,
            )
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), "prewarm caller never returned"

    assert len(errors) == 2
    for exc in errors:
        assert isinstance(exc, worker_tools.WorkerToolError)
        assert not isinstance(exc, source_graph.SourceGraphError)
        assert str(exc).startswith(
            "quality_review_candidate_source_graph_prewarm_error:SourceGraphError:"
        )
