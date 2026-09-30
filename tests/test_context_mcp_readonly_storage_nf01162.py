"""NF-2026-01162: worker context MCP reads and output spill survive storage
this seat cannot write.

Two failures were reported from one worker seat, and they share a shape: a
read-only consumer was defeated by a WRITE it never asked for.

(A) ``ai_memory_search`` failed with ``attempt to write a readonly database``.
    A ``mode=ro`` reader of a WAL database is still expected to create and
    write the ``-shm`` wal-index before it can serve a single row, so a
    database whose sidecars this seat cannot write refuses a pure read.
(B) A large Source Graph reply failed with ``output_spill_store_persist_failed``.
    The spill store is fail-closed by design, but "this seat has no writable
    spill root" is not the same fact as "the payload is corrupt": the first
    should degrade the reply, only the second should fail it.

The sandbox denies chmod/chown, so a genuinely mode-0555 directory is not
reproducible here; both reproductions use the equivalent injected failure the
card allows instead.

(B) needs no injection at all: a spill root path occupied by a regular file
produces the reported ``output_spill_store_persist_failed`` from ordinary
filesystem state. (A) is built from two halves, so that as little as possible
is pretended: the unwritable sidecar state is REAL (a directory on the
``-wal`` path, which is exactly the proof the fallback demands), and only
SQLite's refusal is injected -- at one named seam, with the exact error
reported, and never for the ``immutable=1`` retry. That retry runs against
real SQLite and the real file, so every row the degraded read returns below
came out of the database rather than out of a stub.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import feature_settings  # noqa: E402
from aiworkhub import output_spill_store  # noqa: E402
from aiworkhub import sqlite_readonly  # noqa: E402
from aiworkhub import worker_ai_tools_mcp as worker_tools  # noqa: E402
from aiworkhub.sqlite_readonly import FALLBACK_IMMUTABLE, connect_readonly  # noqa: E402

_MEMORY_ROWS = (
    (
        "nf01162.readonly.storage",
        "worker context mcp reads survive readonly storage nf01162",
        "nf01162,readonly,storage",
        "project",
    ),
    (
        "nf01162.spill.unavailable",
        "output spill degrades to spill_unavailable on a readonly seat",
        "nf01162,spill",
        "project",
    ),
)


# ---------------------------------------------------------------------------
# Fixtures: a database whose read-only open the storage refuses
# ---------------------------------------------------------------------------

def _seed_memory_db(path: Path) -> None:
    """A real memory database with the ``memories``/``memories_fts`` pair."""

    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE memories("
            "id INTEGER PRIMARY KEY, key TEXT, value TEXT, tags TEXT, scope TEXT)"
        )
        con.execute("CREATE VIRTUAL TABLE memories_fts USING fts5(key, value, tags)")
        for index, (key, value, tags, scope) in enumerate(_MEMORY_ROWS, 1):
            con.execute(
                "INSERT INTO memories(id, key, value, tags, scope) VALUES (?,?,?,?,?)",
                (index, key, value, tags, scope),
            )
            con.execute(
                "INSERT INTO memories_fts(rowid, key, value, tags) VALUES (?,?,?,?)",
                (index, key, value, tags),
            )
        con.commit()
    finally:
        con.close()


def _block_wal_sidecar(db_path: Path) -> None:
    """Make the ``-wal`` sidecar state provably unwritable, with no chmod.

    A read-only WAL reader has to create or open ``<db>-wal``, and a
    directory sitting on that path is not something it can open. This is the
    proof the fallback demands before it will degrade an open, and unlike a
    mode-0555 directory it reproduces on Windows and inside a sandbox that
    denies chmod outright.
    """

    db_path.with_name(db_path.name + "-wal").mkdir()


def _refuse_unless_immutable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject the reported refusal at the module's one SQLite seam.

    ``mode=ro`` on a WAL database whose wal-index cannot be created raises
    exactly this error, but standing up a live WAL database (shared-memory
    setup, sidecar recovery, checkpoint-on-close) is not portable enough to
    belong in a test. Only the refusal is injected: the ``immutable=1``
    retry runs against real SQLite and the real file, so every row the
    fallback reads back below is real.
    """

    real = sqlite_readonly._sqlite_connect

    def refusing(uri: str, *, timeout: float) -> sqlite3.Connection:
        if "immutable=1" not in uri:
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return real(uri, timeout=timeout)

    monkeypatch.setattr(sqlite_readonly, "_sqlite_connect", refusing)


def _digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ctx(tmp_path: Path, repo: Path) -> worker_tools.WorkerToolContext:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    ledger = runtime / "audit.jsonl"
    ledger.write_text("", encoding="utf-8")
    key = runtime / "audit.key"
    key.write_bytes(b"k" * 32)
    return worker_tools.WorkerToolContext(
        task_id="needfix-NF-2026-01162",
        runner="codex_worker",
        topic="implementation",
        request_id="b" * 32,
        repo=repo,
        authority_repo=repo,
        source_graph_targets=(),
        session_topic="context_mcp_readonly_storage",
        audit_ledger_path=ledger,
        audit_hmac_key_path=key,
    )


def _bind_memory_db(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setattr(feature_settings, "enabled", lambda repo, feature: True)
    monkeypatch.setattr(
        worker_tools,
        "_resolve_authority_db",
        lambda ctx, *, component, db_id: worker_tools.AuthorityBinding(
            db_path=db_path, authority_source="canonical", authority_state="active"
        ),
    )


def _block_spill_root(repo: Path) -> Path:
    """Occupy the spill root path with a regular file.

    ``spill_text`` starts with ``root.mkdir(parents=True, exist_ok=True)``,
    which refuses a path that exists and is not a directory -- the same
    ``output_spill_store_persist_failed`` a read-only root produces.
    """

    spill = repo / ".aiworkhub" / "spill"
    spill.parent.mkdir(parents=True, exist_ok=True)
    spill.write_text("not a directory", encoding="utf-8")
    return spill


# ---------------------------------------------------------------------------
# (A) the shared read-only open
# ---------------------------------------------------------------------------

def test_nf01162_wal_header_is_what_arms_the_read_only_probe(tmp_path: Path) -> None:
    """The extra probe read is issued for WAL databases and nothing else."""

    plain = tmp_path / "plain.db"
    _seed_memory_db(plain)
    assert sqlite_readonly._is_wal_mode(plain) is False

    header_says_wal = bytearray(plain.read_bytes())
    header_says_wal[18] = 2
    header_says_wal[19] = 2
    wal = tmp_path / "wal.db"
    wal.write_bytes(bytes(header_says_wal))
    assert sqlite_readonly._is_wal_mode(wal) is True


def test_nf01162_sidecar_proof_is_required_before_any_fallback(tmp_path: Path) -> None:
    """Unwritable sidecar state is a fact on disk, not an inference."""

    db = tmp_path / "memory.db"
    _seed_memory_db(db)
    assert sqlite_readonly._sidecar_state_unwritable(db) is False

    _block_wal_sidecar(db)
    assert sqlite_readonly._sidecar_state_unwritable(db) is True


def test_nf01162_readonly_open_on_unwritable_sidecar_reads_and_names_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "memory.db"
    _seed_memory_db(db)
    _block_wal_sidecar(db)
    _refuse_unless_immutable(monkeypatch)

    con = connect_readonly(db)
    try:
        assert sqlite_readonly.fallback_mode(con) == FALLBACK_IMMUTABLE
        assert con.execute("PRAGMA query_only").fetchone()[0] == 1
        keys = [row[0] for row in con.execute("SELECT key FROM memories ORDER BY id")]
        assert keys == [row[0] for row in _MEMORY_ROWS]
    finally:
        con.close()


def test_nf01162_storage_refusal_alone_never_triggers_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without unwritable sidecars the error is real and must still surface."""

    db = tmp_path / "memory.db"
    _seed_memory_db(db)
    _refuse_unless_immutable(monkeypatch)

    with pytest.raises(sqlite3.OperationalError) as exc_info:
        connect_readonly(db)
    assert "attempt to write a readonly database" in str(exc_info.value)


def test_nf01162_normal_readonly_open_is_never_marked_degraded(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    _seed_memory_db(db)

    con = connect_readonly(db)
    try:
        assert sqlite_readonly.fallback_mode(con) is None
        assert con.execute("PRAGMA query_only").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM memories").fetchone()[0] == len(
            _MEMORY_ROWS
        )
    finally:
        con.close()


def test_nf01162_fallback_open_is_still_read_only_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The degraded read must not become a writable seat by another name."""

    source = Path(sqlite_readonly.__file__).read_text(encoding="utf-8")
    # Every statement the shared opener issues, pinned: one read-only PRAGMA
    # and one SELECT. A write statement cannot be added here unnoticed.
    assert [line.strip() for line in source.splitlines() if ".execute(" in line] == [
        'conn.execute("PRAGMA query_only=ON")',
        "conn.execute(_PROBE_SQL).fetchone()",
    ]
    assert sqlite_readonly._PROBE_SQL.upper().startswith("SELECT")

    def _record_opens(mp: pytest.MonkeyPatch, sink: list[str]) -> None:
        """Wrap the CURRENT seam so every URI is recorded, then delegated.

        Installed last so it sits outermost: an injected refusal never reaches
        SQLite, so a recorder underneath one would never see the refused URI.
        """

        beneath = sqlite_readonly._sqlite_connect

        def recording(uri: str, *, timeout: float) -> sqlite3.Connection:
            sink.append(uri)
            return beneath(uri, timeout=timeout)

        mp.setattr(sqlite_readonly, "_sqlite_connect", recording)

    # The access mode is asserted on the URIs SQLite is actually handed rather
    # than on the text of this module: prose naming a writable mode cannot
    # fail that, and an added writable open cannot pass it.
    normal_uris: list[str] = []
    normal_dir = tmp_path / "normal"
    normal_dir.mkdir()
    normal_db = normal_dir / "memory.db"
    _seed_memory_db(normal_db)
    with monkeypatch.context() as normal_leg:
        _record_opens(normal_leg, normal_uris)
        connect_readonly(normal_db).close()
    assert len(normal_uris) == 1
    assert "immutable=1" not in normal_uris[0]

    db = tmp_path / "memory.db"
    _seed_memory_db(db)
    _block_wal_sidecar(db)
    _refuse_unless_immutable(monkeypatch)
    fallback_uris: list[str] = []
    _record_opens(monkeypatch, fallback_uris)
    before_digest = _digest_of(db)
    before_names = sorted(p.name for p in tmp_path.iterdir())

    con = connect_readonly(db)
    try:
        assert sqlite_readonly.fallback_mode(con) == FALLBACK_IMMUTABLE
        with pytest.raises(sqlite3.DatabaseError) as exc_info:
            con.execute("CREATE TABLE injected(x INTEGER)")
        assert "readonly" in str(exc_info.value).lower()
        con.execute("SELECT count(*) FROM memories").fetchone()
    finally:
        con.close()

    assert _digest_of(db) == before_digest
    assert sorted(p.name for p in tmp_path.iterdir()) == before_names

    # The normal open, the refused mode=ro attempt and its immutable retry:
    # every URI this module handed SQLite asked for read-only access.
    every_uri = normal_uris + fallback_uris
    assert len(every_uri) == 3
    assert all("mode=ro" in uri for uri in every_uri)
    assert not any("mode=rw" in uri or "mode=rwc" in uri for uri in every_uri)
    assert "immutable=1" in fallback_uris[-1]


def test_nf01162_missing_database_still_fails_closed_without_a_fallback(
    tmp_path: Path,
) -> None:
    """``immutable=1`` must never paper over a database that is not there."""

    db = tmp_path / "absent.db"
    with pytest.raises(sqlite3.Error):
        connect_readonly(db)
    assert not db.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_nf01162_ai_memory_search_returns_hits_from_unwritable_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported failure: a pure read refused with a write error."""

    repo = tmp_path / "repo"
    repo.mkdir()
    db = tmp_path / "memory.db"
    _seed_memory_db(db)
    _block_wal_sidecar(db)
    _bind_memory_db(monkeypatch, db)
    _refuse_unless_immutable(monkeypatch)

    result = worker_tools.ai_memory_search(
        _ctx(tmp_path, repo), query="readonly storage nf01162", limit=5
    )

    assert result["ok"] is True
    assert result["hit_count"] >= 1
    assert result["degraded"] is True
    assert result["degraded_reason"] == FALLBACK_IMMUTABLE
    assert result["degraded_schema_id"]
    keys = {row["key"] for row in json.loads(result["content"])["results"]}
    assert "nf01162.readonly.storage" in keys


def test_nf01162_ai_memory_search_is_not_degraded_on_writable_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    db = tmp_path / "memory.db"
    _seed_memory_db(db)
    _bind_memory_db(monkeypatch, db)

    result = worker_tools.ai_memory_search(
        _ctx(tmp_path, repo), query="readonly storage nf01162", limit=5
    )

    assert result["ok"] is True
    assert result["hit_count"] >= 1
    assert result["degraded"] is False
    assert result["degraded_reason"] is None


# ---------------------------------------------------------------------------
# (B) the output spill store
# ---------------------------------------------------------------------------

def test_nf01162_unwritable_spill_root_reports_spill_unavailable_not_a_failure(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _block_spill_root(repo)
    text = "payload " * 500

    with pytest.raises(output_spill_store.OutputSpillError) as exc_info:
        output_spill_store.spill_text(text, repo=repo)
    assert "output_spill_store_persist_failed" in str(exc_info.value)

    outcome = output_spill_store.try_spill_text(text, repo=repo)
    assert isinstance(outcome, output_spill_store.SpillUnavailable)
    assert outcome.reason == output_spill_store.SPILL_UNAVAILABLE
    assert outcome.detail
    assert not hasattr(outcome, "locator")


def test_nf01162_try_spill_text_still_fails_closed_on_a_tampered_payload(
    tmp_path: Path,
) -> None:
    """Degrading is only for storage this seat cannot write, never for a lie."""

    repo = tmp_path / "repo"
    repo.mkdir()
    text = "payload " * 500
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    root = repo / ".aiworkhub" / "spill"
    root.mkdir(parents=True)
    (root / f"{digest}.txt").write_text("tampered", encoding="utf-8")

    with pytest.raises(output_spill_store.OutputSpillError) as exc_info:
        output_spill_store.try_spill_text(text, repo=repo)
    assert "output_spill_store_collision" in str(exc_info.value)


def test_nf01162_worker_writable_root_is_used_when_the_repository_root_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _block_spill_root(repo)
    seat = tmp_path / "seat"
    seat.mkdir()
    monkeypatch.setenv("AIWORKHUB_WORKER_SPILL_ROOT", str(seat))
    text = "payload " * 500

    outcome = output_spill_store.try_spill_text(text, repo=repo)

    assert isinstance(outcome, output_spill_store.SpillReceipt)
    assert outcome.locator.startswith("aiworkhub-spill-sha256:")
    assert str(seat) not in outcome.locator
    assert output_spill_store.retrieve_text(outcome.locator, repo=repo) == text


def test_nf01162_writable_spill_root_behaves_exactly_as_today(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    text = "payload " * 500

    outcome = output_spill_store.try_spill_text(text, repo=repo)

    assert isinstance(outcome, output_spill_store.SpillReceipt)
    assert outcome == output_spill_store.spill_text(text, repo=repo)
    assert output_spill_store.retrieve_text(outcome.locator, repo=repo) == text


def test_nf01162_bounded_json_preview_degrades_with_no_locator(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _block_spill_root(repo)
    payload = {
        "mode": "focus",
        "ranked_symbols": [
            {"qualname": f"module.symbol_{index}", "body_preview": "x" * 400}
            for index in range(40)
        ],
    }

    bounded, truncated = worker_tools._canonical_json_output(
        "source_graph", json.dumps(payload), max_bytes=4096, repo=repo
    )

    assert truncated is True
    wrapper = json.loads(bounded)
    assert "spill_locator" not in wrapper
    assert "spill_retrieval_hint" not in wrapper
    assert wrapper["spill_unavailable_reason"] == output_spill_store.SPILL_UNAVAILABLE
    assert wrapper["spill_unavailable_detail"]
    assert wrapper["telemetry"]["spilled_bytes"] == 0
    assert wrapper["telemetry"]["pruned_bytes"] > 0
    assert wrapper["preview"]


def test_nf01162_bounded_json_preview_keeps_its_locator_on_a_writable_root(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = {
        "mode": "focus",
        "ranked_symbols": [
            {"qualname": f"module.symbol_{index}", "body_preview": "x" * 400}
            for index in range(40)
        ],
    }

    bounded, truncated = worker_tools._canonical_json_output(
        "source_graph", json.dumps(payload), max_bytes=4096, repo=repo
    )

    assert truncated is True
    wrapper = json.loads(bounded)
    assert "spill_unavailable_reason" not in wrapper
    assert wrapper["telemetry"]["spilled_bytes"] == wrapper["original_bytes"]
    recovered = output_spill_store.retrieve_text(wrapper["spill_locator"], repo=repo)
    assert json.loads(recovered)["ranked_symbols"] == payload["ranked_symbols"]


def test_nf01162_oversized_source_graph_reply_degrades_instead_of_failing(
    tmp_path: Path,
) -> None:
    """The live over-cap path (``_fit_response_payload``) must not raise."""

    def _dumps(value: dict) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    class _StubEngine:
        @staticmethod
        def _fit_payload_bytes(payload, cap):
            items = list(payload.get("items", []))
            dropped = list(payload.get("fit_dropped") or [])
            while items:
                candidate = {**payload, "items": items, "fit_dropped": dropped}
                if len(_dumps(candidate).encode("utf-8")) <= cap:
                    return candidate
                dropped = [*dropped, f"item_{len(items) - 1}"]
                items = items[:-1]
            return {**payload, "items": [], "fit_dropped": dropped}

    repo = tmp_path / "repo"
    repo.mkdir()
    _block_spill_root(repo)
    output_cap_bytes = 8 * 1024
    payload = {"mode": "focus", "query": "q", "items": ["z" * 40 for _ in range(500)]}
    meta = {"ok": True, "tool": "source_graph_query", "mode": "focus"}

    fitted, dropped_now = worker_tools._fit_response_payload(
        _StubEngine, payload, meta, output_cap_bytes, repo=repo
    )

    assert dropped_now
    assert "spill_locator" not in meta
    assert "spill_retrieval_hint" not in meta
    assert meta["spill_unavailable_reason"] == output_spill_store.SPILL_UNAVAILABLE
    assert meta["telemetry"]["spilled_bytes"] == 0
    assert meta["telemetry"]["pruned_bytes"] > 0

    content = _dumps(fitted)
    result = {
        **meta,
        "truncated": True,
        "outer_truncated": False,
        "bytes": len(content.encode("utf-8")),
        "content": content,
        "content_sha256": "0" * 64,
    }
    assert worker_tools._serialized_response_bytes(result) <= output_cap_bytes
