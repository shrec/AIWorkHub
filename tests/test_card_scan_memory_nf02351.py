"""NF-2026-02351: card-table scans never materialise every card_json at once.

Each changed scan is checked two ways:

* equivalence -- on a seeded task DB mixing statuses, topics, archived rows,
  malformed and non-dict card_json, rework predecessors and quality_review
  bindings, the new implementation returns exactly what a reference copy of
  the OLD (``fetchall()``) implementation, inlined below, returns;
* streaming -- with N large cards that every scan must parse, the tracemalloc
  peak of the new implementation stays within a few card sizes, while the old
  reference (measured the same way, as a control) holds all N at once.
"""

from __future__ import annotations

import gc
import json
import sqlite3
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from aiworkhub import process_launcher_accept_review as accept_review
from aiworkhub import storage_retention, task_retention, task_store

R1 = "req-aaa1"
R2 = "req-bbb2"
R3 = "req-ccc3"


# --------------------------------------------------------------------------
# Reference copies of the OLD implementations (verbatim logic, fetchall()).
# --------------------------------------------------------------------------


def _old_protected_attempt_ids(repo_root: Path):
    sr = storage_retention
    protected: dict[str, str] = {}
    pinned_by: dict[str, list[str]] = {}
    terminal_tasks: set[str] = set()
    unfinished_cards: set[str] = set()
    terminal_needfix_tasks, needfix_lineage_verified = sr._terminal_needfix_task_ids(repo_root)
    try:
        db_path = task_store.canonical_db_path(repo_root)
        conn = task_store.connect_readonly(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT task_id, status, worker_status, archived_at, card_json FROM tasks"
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            status = task_store.canonical_status(dict(row))
            card_id = str(row["task_id"] or "").strip()
            closed_by_needfix = needfix_lineage_verified and card_id in terminal_needfix_tasks
            card_unfinished = status not in sr._FINISHED_STATUSES and not closed_by_needfix
            if card_id and card_unfinished:
                unfinished_cards.add(card_id)
            try:
                card_json = json.loads(row["card_json"] or "{}")
            except json.JSONDecodeError:
                card_json = {}
            if not isinstance(card_json, dict):
                continue
            if card_id and status in sr._FINISHED_STATUSES:
                terminal_tasks.add(card_id)
            if status in ("processing", "review"):
                request_id = str(card_json.get("launch_request_id") or "").strip()
                if request_id and not closed_by_needfix:
                    protected[request_id] = "live_worker"
            if status == "blocked" and not closed_by_needfix:
                for blocked_request_id in sr._blocked_terminal_candidate_request_ids(card_json):
                    protected.setdefault(blocked_request_id, "blocked_terminal_candidate_retained")
            if card_unfinished:
                predecessor = card_json.get("rework_predecessor")
                if isinstance(predecessor, dict):
                    predecessor_id = str(predecessor.get("request_id") or "").strip()
                    if predecessor_id and not sr.has_verified_rework_delta(
                        predecessor, authority_repo=repo_root
                    ):
                        protected.setdefault(predecessor_id, "rework_predecessor_retained")
                        if card_id:
                            holders = pinned_by.setdefault(predecessor_id, [])
                            if card_id not in holders:
                                holders.append(card_id)
    except (task_store.TaskStoreError, sqlite3.Error, OSError):
        return {}, False, {}, set(), set()
    for holders in pinned_by.values():
        holders.sort()
    return protected, True, pinned_by, terminal_tasks, unfinished_cards


def _old_referenced_rework_delta_digests(root: Path) -> set[str]:
    _readiness, db_path = task_store._require_ready(root)
    conn = task_store._connect(db_path, readonly=True)
    try:
        rows = conn.execute(
            "SELECT status, worker_status, archived_at, card_json FROM tasks"
        ).fetchall()
    finally:
        conn.close()
    digests: set[str] = set()
    for row in rows:
        if task_store.canonical_status(dict(row)) in {"finished", "archived", "superseded"}:
            continue
        try:
            stack: list[Any] = [json.loads(str(row["card_json"] or "{}"))]
        except json.JSONDecodeError:
            continue
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if (
                        key in {"artifact_sha256", "digest"}
                        and isinstance(value, str)
                        and len(value) == 64
                        and all(ch in "0123456789abcdef" for ch in value)
                    ):
                        digests.add(value)
                    elif isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(node, list):
                stack.extend(node)
    return digests


def _old_rework_pinned(conn: sqlite3.Connection) -> set[str]:
    pinned: set[str] = set()
    rows = conn.execute(
        "SELECT status, worker_status, archived_at, card_json FROM tasks"
    ).fetchall()
    for row in rows:
        if task_store.canonical_status(dict(row)) not in {"pending", "processing", "review", "blocked"}:
            continue
        card = task_store._row_card(row["card_json"])
        predecessor = card.get("rework_predecessor")
        if isinstance(predecessor, dict):
            request_id = str(predecessor.get("request_id") or "").strip()
            if request_id:
                pinned.add(request_id)
    return pinned


def _old_live_rework_references(root: Path, request_id: str):
    tr = task_retention
    if not tr._BATCH_ID_RE.fullmatch(str(request_id or "")):
        return [], False
    try:
        connection = tr._connect(root, readonly=True)
    except (task_store.TaskStoreError, sqlite3.Error, OSError):
        return [], False
    try:
        rows = connection.execute(
            "SELECT task_id, status, worker_status, archived_at, card_json FROM tasks"
        ).fetchall()
        pins: list[str] = []
        for row in rows:
            status = task_store.canonical_status(dict(row))
            if status in tr._FINISHED_CLEANUP_STATUSES:
                continue
            try:
                card = json.loads(row["card_json"] or "{}")
            except json.JSONDecodeError:
                return [], False
            if not isinstance(card, dict):
                return [], False
            predecessor = card.get("rework_predecessor")
            if (
                isinstance(predecessor, dict)
                and str(predecessor.get("request_id") or "").strip() == request_id
            ):
                card_id = str(row["task_id"] or "").strip()
                if card_id:
                    pins.append(card_id)
        return sorted(set(pins)), True
    except sqlite3.Error:
        return [], False
    finally:
        connection.close()


def _old_live_process_holders(root: Path, request_id: str):
    tr = task_retention
    if not tr._BATCH_ID_RE.fullmatch(str(request_id or "")):
        return [], False
    identity, identity_verified = tr._request_process_identity_state(root, request_id)
    if not identity_verified:
        return [], False
    try:
        connection = tr._connect(root, readonly=True)
    except (task_store.TaskStoreError, sqlite3.Error, OSError):
        return [], False
    try:
        rows = connection.execute(
            "SELECT task_id, status, worker_status, archived_at, card_json FROM tasks"
        ).fetchall()
        holders: list[str] = []
        for row in rows:
            status = task_store.canonical_status(dict(row))
            worker_status = str(row["worker_status"] or "").strip().lower()
            supervisor_live = worker_status in tr._LIVE_SUPERVISOR_STATUSES
            process_live = status in tr._LIVE_PROCESS_STATUSES
            try:
                card = json.loads(row["card_json"] or "{}")
            except json.JSONDecodeError:
                if process_live or supervisor_live:
                    return [], False
                continue
            if not isinstance(card, dict):
                if process_live or supervisor_live:
                    return [], False
                continue
            bound_ids = {
                str(card.get("launch_request_id") or "").strip(),
                str(card.get("accepted_request_id") or "").strip(),
            }
            if request_id not in bound_ids:
                continue
            if not process_live and not supervisor_live and identity != "live":
                continue
            card_id = str(row["task_id"] or "").strip()
            if card_id:
                holders.append(card_id)
        return sorted(set(holders)), True
    except sqlite3.Error:
        return [], False
    finally:
        connection.close()


def _old_bound_reviewer_rows(repo: Path, parent_task_id: str, parent_request_id: str):
    try:
        _readiness, db_path = task_store._require_ready(repo)
        conn = task_store._connect(db_path, readonly=True)
    except Exception:  # noqa: BLE001
        return []
    try:
        rows = conn.execute(
            "SELECT task_id, card_json FROM tasks WHERE topic='quality_review'"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()
    bound: list[dict[str, Any]] = []
    for row in rows:
        try:
            card = json.loads(row["card_json"] or "{}")
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(card, dict):
            continue
        terminal = card.get("terminal_review")
        terminal = terminal if isinstance(terminal, dict) else {}
        terminal_evidence = terminal.get("evidence")
        terminal_evidence = terminal_evidence if isinstance(terminal_evidence, dict) else {}
        terminal_binding = terminal_evidence.get("quality_review")
        root_binding = card.get("quality_review")
        if not isinstance(terminal_binding, dict):
            terminal_binding = {}
        if not isinstance(root_binding, dict):
            root_binding = {}
        if terminal_binding and root_binding and terminal_binding != root_binding:
            continue
        binding = terminal_binding or root_binding
        if (
            str(binding.get("target_task_id") or "") != str(parent_task_id)
            or str(binding.get("target_request_id") or "") != str(parent_request_id)
        ):
            continue
        task_id = str(row["task_id"] or "")
        if not task_id:
            continue
        receipt = terminal_evidence.get("quality_review_receipt")
        bound.append(
            {
                "task_id": task_id,
                "lens": str(binding.get("lens") or ""),
                "packet_sha256": str(binding.get("packet_sha256") or ""),
                "terminal_substatus": str(terminal.get("substatus") or ""),
                "receipt": receipt if isinstance(receipt, dict) else None,
            }
        )
    return sorted(bound, key=lambda entry: entry["task_id"])


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _insert(
    repo: Path,
    rows: list[tuple[str, str, str, str, str, str]],
) -> None:
    """rows: (task_id, topic, status, worker_status, archived_at, raw card_json)."""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(task_store.canonical_db_path(repo)))
    try:
        conn.executemany(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, archived_at, "
            "card_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(tid, "claude", topic, st, ws, arch, cj, now, now) for tid, topic, st, ws, arch, cj in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _binding(target_task: str, target_request: str, lens: str) -> dict[str, Any]:
    return {
        "target_task_id": target_task,
        "target_request_id": target_request,
        "lens": lens,
        "packet_sha256": "p" * 8,
    }


def _mixed_rows() -> list[tuple[str, str, str, str, str, str]]:
    d = json.dumps
    return [
        ("t-pend-pin", "code", "pending", "unclaimed", "",
         d({"rework_predecessor": {"request_id": R1}, "launch_request_id": R2})),
        ("t-proc", "code", "processing", "claimed", "",
         d({"launch_request_id": R2, "rework_predecessor": {"request_id": f" {R1} "}})),
        ("t-review", "code", "review", "unclaimed", "", d({"launch_request_id": R3})),
        ("t-claimed", "code", "pending", "claimed", "", d({"launch_request_id": R1})),
        ("t-blocked", "code", "blocked", "unclaimed", "", d({
            "launch_request_id": R3,
            "terminal_substatus": "finalize_failed",
            "terminal_failure": {"substatus": "finalize_failed", "request_id": R3},
        })),
        ("t-fin", "code", "finished", "unclaimed", "", d({
            "accepted_request_id": R1,
            "rework_predecessor": {"request_id": R2},
            "delta": {"digest": "a" * 64},
        })),
        ("t-done", "code", "pending", "done", "", d({"accepted_request_id": R2})),
        ("t-arch", "code", "pending", "unclaimed", "2026-01-01T00:00:00+00:00",
         d({"rework_predecessor": {"request_id": R1}, "launch_request_id": R1})),
        ("t-super", "code", "superseded", "unclaimed", "", d({"rework_predecessor": {"request_id": R3}})),
        ("t-fin-bad", "code", "finished", "unclaimed", "", "{not json"),
        ("t-fin-list", "code", "finished", "unclaimed", "", "[1, 2]"),
        ("t-fin-empty", "code", "finished", "unclaimed", "", ""),
        ("t-fin-nonascii", "code", "finished", "unclaimed", "",
         json.dumps({"note": "café", "launch_request_id": R1}, ensure_ascii=False)),
        ("t-pend-digest", "code", "pending", "unclaimed", "", d({
            "rework_predecessor": {"request_id": R2},
            "rework_delta": {
                "artifact_sha256": "b" * 64,
                "nested": [{"digest": "c" * 64}, {"digest": "short"}, {"digest": "Z" * 64}],
            },
        })),
        ("t-pend-empty", "code", "pending", "unclaimed", "", ""),
        ("q-terminal", "quality_review", "finished", "unclaimed", "", d({
            "terminal_review": {
                "substatus": "accepted",
                "evidence": {
                    "quality_review": _binding("t-proc", R2, "L1"),
                    "quality_review_receipt": {"ok": True},
                },
            },
        })),
        ("q-root", "quality_review", "pending", "unclaimed", "",
         d({"quality_review": _binding("t-proc", R2, "L2")})),
        ("q-disagree", "quality_review", "pending", "unclaimed", "", d({
            "quality_review": _binding("t-proc", R2, "L3"),
            "terminal_review": {"evidence": {"quality_review": _binding("t-proc", R2, "L4")}},
        })),
        ("q-other", "quality_review", "pending", "unclaimed", "",
         d({"quality_review": _binding("t-proc", R3, "L5")})),
        ("q-bad", "quality_review", "finished", "unclaimed", "", "{" + R2),
        ("q-list", "quality_review", "finished", "unclaimed", "", d([R2])),
    ]


def _malformed_live_rows() -> list[tuple[str, str, str, str, str, str]]:
    return [
        ("t-live-bad", "code", "pending", "unclaimed", "", "{broken"),
        ("t-live-list", "code", "processing", "unclaimed", "", "[]"),
    ]


@pytest.fixture(params=["mixed", "malformed_live"])
def seeded_repo(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    repo = _repo(tmp_path)
    rows = _mixed_rows()
    if request.param == "malformed_live":
        rows += _malformed_live_rows()
    _insert(repo, rows)
    return repo


# --------------------------------------------------------------------------
# Equivalence
# --------------------------------------------------------------------------


def test_protected_attempt_ids_matches_old(seeded_repo: Path) -> None:
    new = storage_retention._protected_attempt_ids(seeded_repo)
    assert new == _old_protected_attempt_ids(seeded_repo)
    assert new[1] is True
    assert "t-fin-list" not in new[3]  # quirk kept: non-dict card is never terminal
    assert "t-fin-bad" in new[3]  # malformed parses to {} and stays terminal


def test_referenced_rework_delta_digests_matches_old(seeded_repo: Path) -> None:
    new = task_store.referenced_rework_delta_digests(seeded_repo)
    assert new == _old_referenced_rework_delta_digests(seeded_repo)
    assert new == {"b" * 64, "c" * 64}


def test_rework_pinned_predecessors_matches_old(seeded_repo: Path) -> None:
    _readiness, db_path = task_store._require_ready(seeded_repo)
    conn = task_store._connect(db_path, readonly=True)
    try:
        new = task_store._rework_pinned_predecessor_request_ids(conn)
        old = _old_rework_pinned(conn)
    finally:
        conn.close()
    assert new == old
    assert new == {R1, R2}


@pytest.mark.parametrize("request_id", [R1, R2, R3, "req-none9", "bad id!"])
def test_live_rework_references_matches_old(seeded_repo: Path, request_id: str) -> None:
    assert task_retention.live_rework_references(seeded_repo, request_id) == (
        _old_live_rework_references(seeded_repo, request_id)
    )


@pytest.mark.parametrize("identity", ["live", "idle"])
@pytest.mark.parametrize("request_id", [R1, R2, R3, "req-none9"])
def test_live_process_holders_matches_old(
    seeded_repo: Path, request_id: str, identity: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        task_retention, "_request_process_identity_state", lambda _root, _rid: (identity, True)
    )
    assert task_retention.live_process_holders(seeded_repo, request_id) == (
        _old_live_process_holders(seeded_repo, request_id)
    )


@pytest.mark.parametrize(
    ("parent_task", "parent_request"),
    [("t-proc", R2), ("t-proc", R3), ("t-proc", ""), ("", ""), ("t-proc", 'req "q"')],
)
def test_bound_reviewer_rows_matches_old(
    seeded_repo: Path, parent_task: str, parent_request: str
) -> None:
    assert accept_review.bound_reviewer_rows(seeded_repo, parent_task, parent_request) == (
        _old_bound_reviewer_rows(seeded_repo, parent_task, parent_request)
    )


def test_seeded_mixed_db_exercises_positive_results(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    _insert(repo, _mixed_rows())
    assert task_retention.live_rework_references(repo, R1) == (["t-pend-pin", "t-proc"], True)
    monkeypatch.setattr(
        task_retention, "_request_process_identity_state", lambda _root, _rid: ("idle", True)
    )
    assert task_retention.live_process_holders(repo, R2) == (["t-proc"], True)
    lenses = [row["lens"] for row in accept_review.bound_reviewer_rows(repo, "t-proc", R2)]
    assert sorted(lenses) == ["L1", "L2"]


def test_malformed_live_card_still_fails_closed(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    _insert(repo, _mixed_rows() + _malformed_live_rows())
    assert task_retention.live_rework_references(repo, R1) == ([], False)
    monkeypatch.setattr(
        task_retention, "_request_process_identity_state", lambda _root, _rid: ("idle", True)
    )
    assert task_retention.live_process_holders(repo, R2) == ([], False)


def test_sqlite_errors_fail_closed(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    _insert(repo, _mixed_rows())
    monkeypatch.setattr(task_store, "card_json_unrelated_sql", lambda _conn: "(no_such_fn())")
    monkeypatch.setattr(
        task_retention, "_request_process_identity_state", lambda _root, _rid: ("idle", True)
    )
    assert task_retention.live_rework_references(repo, R1) == ([], False)
    assert task_retention.live_process_holders(repo, R2) == ([], False)


# --------------------------------------------------------------------------
# Streaming: peak traced memory stays far below N x card size
# --------------------------------------------------------------------------

_CARD_BYTES = 256 * 1024
_CARD_COUNT = 48
_MEM_REQUEST = "req-mem1"


def _peak_bytes(fn: Callable[[], object]) -> int:
    gc.collect()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        fn()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


@pytest.fixture()
def large_card_repo(tmp_path: Path) -> Path:
    repo = _repo(tmp_path)
    rows = []
    for index in range(_CARD_COUNT):
        card = {
            "launch_request_id": _MEM_REQUEST,
            "rework_predecessor": {"request_id": _MEM_REQUEST},
            "quality_review": _binding("t-parent", _MEM_REQUEST, f"L{index}"),
            "rework_delta": {"digest": f"{index:064x}"},
            "pad": f"{index:06d}" + "x" * _CARD_BYTES,
        }
        rows.append((f"m-{index:03d}", "quality_review", "pending", "unclaimed", "", json.dumps(card)))
    _insert(repo, rows)
    return repo


def _pinned(repo: Path, impl: Callable[[sqlite3.Connection], set[str]]) -> set[str]:
    _readiness, db_path = task_store._require_ready(repo)
    conn = task_store._connect(db_path, readonly=True)
    try:
        return impl(conn)
    finally:
        conn.close()


def test_scans_stream_instead_of_materialising(
    large_card_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = large_card_repo
    monkeypatch.setattr(
        task_retention, "_request_process_identity_state", lambda _root, _rid: ("live", True)
    )
    pairs: dict[str, tuple[Callable[[], object], Callable[[], object]]] = {
        "protected_attempt_ids": (
            lambda: storage_retention._protected_attempt_ids(repo),
            lambda: _old_protected_attempt_ids(repo),
        ),
        "referenced_rework_delta_digests": (
            lambda: task_store.referenced_rework_delta_digests(repo),
            lambda: _old_referenced_rework_delta_digests(repo),
        ),
        "rework_pinned_predecessors": (
            lambda: _pinned(repo, task_store._rework_pinned_predecessor_request_ids),
            lambda: _pinned(repo, _old_rework_pinned),
        ),
        "live_rework_references": (
            lambda: task_retention.live_rework_references(repo, _MEM_REQUEST),
            lambda: _old_live_rework_references(repo, _MEM_REQUEST),
        ),
        "live_process_holders": (
            lambda: task_retention.live_process_holders(repo, _MEM_REQUEST),
            lambda: _old_live_process_holders(repo, _MEM_REQUEST),
        ),
        "bound_reviewer_rows": (
            lambda: accept_review.bound_reviewer_rows(repo, "t-parent", _MEM_REQUEST),
            lambda: _old_bound_reviewer_rows(repo, "t-parent", _MEM_REQUEST),
        ),
    }
    total = _CARD_COUNT * _CARD_BYTES
    for name, (new_fn, old_fn) in pairs.items():
        # Every large card is actually read by both implementations.
        assert new_fn() == old_fn(), name
        new_peak = _peak_bytes(new_fn)
        old_peak = _peak_bytes(old_fn)
        # Control: the old fetchall() holds every card's text at once.
        assert old_peak >= total, (name, old_peak)
        # Streamed: only a handful of cards (row text + parsed copy) alive.
        assert new_peak < 8 * _CARD_BYTES, (name, new_peak, old_peak)
