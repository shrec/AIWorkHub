"""NF-2026-01616: the cost ledger reads only the SQLite columns it consumes."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import cost_ledger  # noqa: E402
from aiworkhub import task_store  # noqa: E402

_EPOCH = datetime(2026, 8, 3, tzinfo=timezone.utc)
_EVENT_OFFSET_SECONDS = 1_000
_ATTEMPT_AT = "2026-08-03T00:05:00+00:00"
_BLOB = "x" * 200_000


def _at(offset_seconds: int) -> str:
    return (_EPOCH + timedelta(seconds=offset_seconds)).isoformat()


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


# (task_id, tasks.topic column, card_json, archived_at)
_TASKS: tuple[tuple[str, str, Any, str], ...] = (
    ("T-ACC-CODE", "task_mcp", {"project_context": {"task_type": "code"}, "risk_tier": "high"}, ""),
    ("T-REJ-RESEARCH", "task_mcp", {"project_context": {"task_type": "research"}, "risk_tier": " Low "}, ""),
    (
        "T-ARCH-REJECT",
        "task_mcp",
        {"project_context": {"task_type": "data_classification"}, "risk_tier": "critical"},
        _at(900),
    ),
    ("T-ARCH-OTHER", "task_mcp", {"project_context": {"task_type": "code"}, "risk_tier": "medium"}, _at(900)),
    ("T-SUP-REJECT", "task_mcp", {"risk_tier": "medium"}, ""),
    ("T-SUP-OTHER", "task_mcp", {"project_context": {"task_type": "research"}}, ""),
    ("T-INVALID-AFTER-ACCEPT", "task_mcp", {"project_context": {"task_type": "code"}, "risk_tier": "low"}, ""),
    ("T-INVALID-ONLY", "task_mcp", {"project_context": {"task_type": "code"}}, ""),
    ("T-FLIP", "task_mcp", {"project_context": {"task_type": "research"}, "risk_tier": "high"}, ""),
    ("T-QR-COLUMN", "quality_review", {"project_context": {"task_type": "code"}, "risk_tier": "low"}, ""),
    ("T-QR-CARD-FALLBACK", "", {"topic": "quality_review", "project_context": {"task_type": "code"}}, ""),
    ("T-QR-PADDED", " quality_review ", {"project_context": {"task_type": "code"}}, ""),
    (
        "T-TOPIC-COLUMN-WINS",
        "task_mcp",
        {"topic": "quality_review", "project_context": {"task_type": "research"}},
        "",
    ),
    ("T-NO-CONTEXT", "task_mcp", {}, ""),
    ("T-BAD-CONTEXT", "task_mcp", {"project_context": "code", "risk_tier": "urgent"}, ""),
    ("T-INVALID-CARD", "task_mcp", "not json{", ""),
    ("T-EMPTY-CARD", "task_mcp", "", ""),
    ("T-LIST-CARD", "task_mcp", [1, 2, 3], ""),
    ("T-NON-STRING", "", {"topic": 5, "project_context": {"task_type": 5}, "risk_tier": 7}, ""),
    ("T-NORMALIZED", "task_mcp", {"project_context": {"task_type": " CODE "}, "risk_tier": "CRITICAL"}, ""),
    (
        "T-BIG-CARD",
        "task_mcp",
        {"filler": _BLOB, "project_context": {"task_type": "research"}, "risk_tier": "medium"},
        "",
    ),
)

# (task_id, event, payload_json); a str payload is stored verbatim.
_DECISION_EVENTS: tuple[tuple[str, str, Any], ...] = (
    ("T-ACC-CODE", "accept_review", {"summary": "ok"}),
    ("T-REJ-RESEARCH", "reject_review", {"summary": "no"}),
    ("T-ARCH-REJECT", "archived", {"reason": "reject_review: scope drift"}),
    ("T-ARCH-OTHER", "archived", {"reason": "lifecycle_cleanup"}),
    ("T-SUP-REJECT", "superseded", {"reason": "reject_review: replaced by retry"}),
    ("T-SUP-OTHER", "superseded", {"reason": "operator_supersede"}),
    ("T-INVALID-AFTER-ACCEPT", "accept_review", {}),
    ("T-INVALID-AFTER-ACCEPT", "archived", "not json{"),
    ("T-INVALID-ONLY", "archived", "not json{"),
    ("T-FLIP", "reject_review", {}),
    ("T-FLIP", "accept_review", {}),
    ("T-QR-COLUMN", "accept_review", {}),
    ("T-QR-CARD-FALLBACK", "accept_review", {}),
    ("T-QR-PADDED", "accept_review", {}),
    ("T-TOPIC-COLUMN-WINS", "reject_review", {}),
    ("T-NO-CONTEXT", "accept_review", {}),
    ("T-BAD-CONTEXT", "accept_review", {}),
    ("T-INVALID-CARD", "reject_review", {}),
    ("T-EMPTY-CARD", "accept_review", {}),
    ("T-LIST-CARD", "reject_review", {}),
    ("T-NON-STRING", "accept_review", {}),
    ("T-NORMALIZED", "accept_review", {}),
    ("T-BIG-CARD", "accept_review", {}),
    ("T-GONE", "accept_review", {}),
)

_PAYLOAD_EDGE_EVENTS: tuple[tuple[str, str, Any], ...] = (
    ("P-PREFIX-ONLY", "archived", {"reason": "reject_review:"}),
    ("P-CASE", "archived", {"reason": "Reject_review: shouting"}),
    ("P-LEADING-SPACE", "superseded", {"reason": " reject_review: padded"}),
    ("P-NULL-REASON", "archived", {"reason": None}),
    ("P-NUMBER-REASON", "archived", {"reason": 5}),
    ("P-LIST-REASON", "archived", {"reason": ["reject_review: nested"]}),
    ("P-NO-REASON", "archived", {"note": "reject_review: elsewhere"}),
    ("P-EMPTY-PAYLOAD", "archived", ""),
    ("P-UNICODE", "archived", '{"reason": "reject_review: café ☃"}'),
    ("P-ESCAPED", "superseded", '{"reason": "\\u0072eject_review: escaped"}'),
    ("BIG-ACCEPT", "accept_review", {"blob": _BLOB}),
    ("BIG-REJECT", "reject_review", {"blob": _BLOB}),
    ("BIG-ARCHIVE-OTHER", "archived", {"blob": _BLOB, "reason": "lifecycle_cleanup"}),
    ("BIG-ARCHIVE-REJECT", "archived", {"blob": _BLOB, "reason": "reject_review: big"}),
    ("", "accept_review", {}),
)

_EXPECTED_DECISIONS = {
    "T-ACC-CODE": ("accepted", "accept_review"),
    "T-REJ-RESEARCH": ("rejected", "reject_review"),
    "T-ARCH-REJECT": ("rejected", "archived"),
    "T-SUP-REJECT": ("rejected", "superseded"),
    "T-INVALID-AFTER-ACCEPT": ("accepted", "accept_review"),
    "T-FLIP": ("accepted", "accept_review"),
    "T-QR-COLUMN": ("accepted", "accept_review"),
    "T-QR-CARD-FALLBACK": ("accepted", "accept_review"),
    "T-QR-PADDED": ("accepted", "accept_review"),
    "T-TOPIC-COLUMN-WINS": ("rejected", "reject_review"),
    "T-NO-CONTEXT": ("accepted", "accept_review"),
    "T-BAD-CONTEXT": ("accepted", "accept_review"),
    "T-INVALID-CARD": ("rejected", "reject_review"),
    "T-EMPTY-CARD": ("accepted", "accept_review"),
    "T-LIST-CARD": ("rejected", "reject_review"),
    "T-NON-STRING": ("accepted", "accept_review"),
    "T-NORMALIZED": ("accepted", "accept_review"),
    "T-BIG-CARD": ("accepted", "accept_review"),
    "T-GONE": ("accepted", "accept_review"),
    "P-PREFIX-ONLY": ("rejected", "archived"),
    "P-UNICODE": ("rejected", "archived"),
    "P-ESCAPED": ("rejected", "superseded"),
    "BIG-ACCEPT": ("accepted", "accept_review"),
    "BIG-REJECT": ("rejected", "reject_review"),
    "BIG-ARCHIVE-REJECT": ("rejected", "archived"),
}

_EXPECTED_FAMILY_RISK = {
    "T-ACC-CODE": ("code", "high"),
    "T-REJ-RESEARCH": ("research", "low"),
    "T-ARCH-REJECT": ("linguistic", "critical"),
    "T-ARCH-OTHER": ("code", "medium"),
    "T-SUP-REJECT": ("unknown", "medium"),
    "T-SUP-OTHER": ("research", "unknown"),
    "T-INVALID-AFTER-ACCEPT": ("code", "low"),
    "T-INVALID-ONLY": ("code", "unknown"),
    "T-FLIP": ("research", "high"),
    "T-QR-COLUMN": ("review", "low"),
    "T-QR-CARD-FALLBACK": ("review", "unknown"),
    "T-QR-PADDED": ("review", "unknown"),
    "T-TOPIC-COLUMN-WINS": ("research", "unknown"),
    "T-NO-CONTEXT": ("unknown", "unknown"),
    "T-BAD-CONTEXT": ("unknown", "unknown"),
    "T-INVALID-CARD": ("unknown", "unknown"),
    "T-EMPTY-CARD": ("unknown", "unknown"),
    "T-LIST-CARD": ("unknown", "unknown"),
    "T-NON-STRING": ("unknown", "unknown"),
    "T-NORMALIZED": ("code", "critical"),
    "T-BIG-CARD": ("research", "medium"),
}

# (model, route, family, risk) -> (accepted_outcomes, rejected_outcomes)
_EXPECTED_BUCKETS = {
    ("model-a", "codex_cli", "code", "high"): (1, 0),
    ("model-a", "codex_cli", "code", "low"): (1, 0),
    ("model-a", "codex_cli", "code", "critical"): (1, 0),
    ("model-a", "codex_cli", "research", "low"): (0, 1),
    ("model-a", "codex_cli", "research", "high"): (1, 0),
    ("model-a", "codex_cli", "research", "medium"): (1, 0),
    ("model-a", "codex_cli", "research", "unknown"): (0, 1),
    ("model-a", "codex_cli", "linguistic", "critical"): (0, 1),
    ("model-a", "codex_cli", "review", "low"): (1, 0),
    ("model-a", "codex_cli", "review", "unknown"): (2, 0),
    ("model-a", "codex_cli", "unknown", "medium"): (0, 1),
    ("model-a", "codex_cli", "unknown", "unknown"): (5, 2),
}


def _seed(
    root: Path,
    *,
    tasks: tuple[tuple[str, str, Any, str], ...] = (),
    events: tuple[tuple[str, str, Any], ...] = (),
) -> None:
    task_store.initialize_repository(root)
    _readiness, db_path = task_store._require_ready(root)
    conn = sqlite3.connect(db_path)
    try:
        for index, (task_id, topic, card, archived_at) in enumerate(tasks):
            conn.execute(
                "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
                "objective, card_json, created_at, updated_at, archived_at) "
                "VALUES (?, 'codex_worker', ?, 'finished', 'done', '', '', ?, ?, ?, ?)",
                (task_id, topic, _text(card), _at(index), _at(index), archived_at),
            )
        for index, (task_id, event, payload) in enumerate(events):
            conn.execute(
                "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
                "VALUES (?, ?, 'codex_worker', ?, ?)",
                (task_id, event, _text(payload), _at(_EVENT_OFFSET_SECONDS + index)),
            )
        conn.commit()
    finally:
        conn.close()


def _legacy_latest_manager_decisions(
    root: Path | str, *, limit: int = 50_000
) -> dict[str, dict[str, str]]:
    """The pre-NF-2026-01616 implementation, kept as the equivalence oracle."""
    _readiness, db_path = task_store._require_ready(root)
    conn = task_store._connect(db_path, readonly=True)
    try:
        rows = conn.execute(
            "SELECT task_id, event, payload_json, created_at FROM task_events "
            "WHERE event IN ('accept_review','reject_review','archived','superseded') "
            "ORDER BY event_id DESC LIMIT ?",
            (max(1, min(int(limit), 50_000)),),
        ).fetchall()
    finally:
        conn.close()
    decisions: dict[str, dict[str, str]] = {}
    for row in rows:
        task_id = str(row["task_id"] or "")
        if not task_id or task_id in decisions:
            continue
        event = str(row["event"] or "")
        if event == "accept_review":
            decision = "accepted"
        elif event == "reject_review":
            decision = "rejected"
        else:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except json.JSONDecodeError:
                continue
            if not str(payload.get("reason") or "").startswith("reject_review:"):
                continue
            decision = "rejected"
        decisions[task_id] = {
            "decision": decision,
            "event": event,
            "created_at": str(row["created_at"] or ""),
        }
    return decisions


def _usage_events() -> list[dict[str, Any]]:
    task_ids = [task_id for task_id, *_rest in _TASKS] + ["T-GONE"]
    events: list[dict[str, Any]] = [
        {
            "task_id": task_id,
            "runner": "codex_worker",
            "topic": "task_mcp",
            "model": "model-a",
            "provider": "codex",
            "role": "worker",
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cost_usd": 0.1,
            "cost_observed": True,
            "usage_observed": True,
            "source": "task_mcp_launcher",
            "note": f"task_mcp_request:req-{task_id}",
            "created_at": _ATTEMPT_AT,
        }
        for task_id in task_ids
    ]
    events.append({
        "task_id": "T-ACC-CODE",
        "runner": "codex_reviewer",
        "topic": "quality_review",
        "model": "review-model",
        "provider": "claude",
        "role": "reviewer",
        "total_tokens": 10,
        "cost_usd": 0.5,
        "cost_observed": True,
        "usage_observed": True,
        "created_at": _ATTEMPT_AT,
    })
    return events


def _bucket_outcomes(view: dict[str, Any]) -> dict[tuple[str, str, str, str], tuple[int, int]]:
    return {
        (model, route, family, risk): (row["accepted_outcomes"], row["rejected_outcomes"])
        for model, routes in view["routes"].items()
        for route, families in routes.items()
        for family, risks in families.items()
        for risk, row in risks.items()
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


class _ColumnSpy:
    def __init__(self, conn: sqlite3.Connection, seen: list[list[str]]) -> None:
        self._conn = conn
        self._seen = seen

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        cursor = self._conn.execute(sql, params)
        if cursor.description:
            self._seen.append([column[0] for column in cursor.description])
        return cursor

    def close(self) -> None:
        self._conn.close()


def _spy_on_selected_columns(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real_connect = task_store._connect

    def spying_connect(*args: Any, **kwargs: Any) -> _ColumnSpy:
        return _ColumnSpy(real_connect(*args, **kwargs), seen)

    monkeypatch.setattr(task_store, "_connect", spying_connect)
    return seen


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("nf01616") / "repo"
    root.mkdir()
    _seed(root, tasks=_TASKS, events=_DECISION_EVENTS + _PAYLOAD_EDGE_EVENTS)
    return root


@pytest.fixture
def ledger_inputs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        task_store, "list_usage_events", lambda _root, limit=10_000: _usage_events()
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_home_missing"))


def test_build_cost_ledger_output_is_identical_to_full_reads(
    repo: Path, ledger_inputs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow = cost_ledger.build_cost_ledger(repo_root=repo, include_tasks=True)

    with monkeypatch.context() as legacy:
        legacy.setattr(task_store, "latest_manager_decisions", _legacy_latest_manager_decisions)
        legacy.setattr(
            task_store,
            "list_task_routing_facts",
            lambda root, *, limit=500: task_store.list_task_cards(root, limit=limit),
        )
        wide = cost_ledger.build_cost_ledger(repo_root=repo, include_tasks=True)

    assert _canonical_json(narrow) == _canonical_json(wide)
    view = narrow["cost_per_accepted_outcome"]
    assert _bucket_outcomes(view) == _EXPECTED_BUCKETS
    matched = sum(accepted + rejected for accepted, rejected in _EXPECTED_BUCKETS.values())
    # build_cost_ledger scopes decisions to task ids that have usage rows.
    scoped = _EXPECTED_DECISIONS.keys() & {row["task_id"] for row in _usage_events()}
    assert matched + view["unmatched_decisions"] == len(scoped)


def test_build_cost_ledger_never_decodes_full_task_cards(
    repo: Path, ledger_inputs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("build_cost_ledger must not decode full task cards")

    calls: list[dict[str, Any]] = []
    real_facts = task_store.list_task_routing_facts

    def recording_facts(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs)
        return real_facts(*args, **kwargs)

    monkeypatch.setattr(task_store, "list_task_cards", forbidden)
    monkeypatch.setattr(task_store, "_decode_task_card", forbidden)
    monkeypatch.setattr(task_store, "list_task_routing_facts", recording_facts)

    result = cost_ledger.build_cost_ledger(repo_root=repo, include_tasks=True)

    assert calls == [{"limit": 5000}]
    assert _bucket_outcomes(result["cost_per_accepted_outcome"]) == _EXPECTED_BUCKETS


def test_build_cost_ledger_tolerates_an_unready_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_store, "list_usage_events", lambda _root, limit=10_000: [])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_home_missing"))

    result = cost_ledger.build_cost_ledger(repo_root=tmp_path)

    assert result["cost_per_accepted_outcome"]["routes"] == {}
    with pytest.raises(task_store.StorageNotReadyError):
        task_store.list_task_routing_facts(tmp_path)


@pytest.mark.parametrize("limit", [1, 2, 5, 9, 17, 40, 50_000, 10**9, 0, -1])
def test_latest_manager_decisions_matches_the_full_payload_reader(repo: Path, limit: int) -> None:
    narrow = task_store.latest_manager_decisions(repo, limit=limit)
    legacy = _legacy_latest_manager_decisions(repo, limit=limit)

    assert list(narrow.items()) == list(legacy.items())


def test_latest_manager_decisions_pins_reason_and_overwrite_semantics(repo: Path) -> None:
    decisions = task_store.latest_manager_decisions(repo)

    assert {
        task_id: (row["decision"], row["event"]) for task_id, row in decisions.items()
    } == _EXPECTED_DECISIONS
    assert all(row["created_at"] for row in decisions.values())
    assert decisions == _legacy_latest_manager_decisions(repo)


def test_valid_non_object_payloads_are_skipped_instead_of_fatal(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _seed(
        root,
        events=(
            ("N-OLDER-ACCEPT", "accept_review", {}),
            ("N-OLDER-ACCEPT", "archived", "[]"),
            ("N-LIST", "archived", "[]"),
            ("N-STRING", "superseded", '"reject_review: not an object"'),
            ("N-NULL", "archived", "null"),
            ("N-NUMBER", "archived", "5"),
            ("N-REJECT", "archived", {"reason": "reject_review: real"}),
        ),
    )

    decisions = task_store.latest_manager_decisions(root)

    assert {task_id: row["event"] for task_id, row in decisions.items()} == {
        "N-REJECT": "archived",
        "N-OLDER-ACCEPT": "accept_review",
    }


def test_unpaired_surrogate_reason_is_not_fatal(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    # json_valid accepts a lone surrogate escape, but SQLite unescapes it into
    # bytes that are not UTF-8, so the reason must never cross as TEXT.
    _seed(
        root,
        events=(
            ("S-ARCHIVED", "archived", r'{"reason": "\ud800"}'),
            ("S-REJECT", "superseded", r'{"reason": "reject_review: \ud800"}'),
        ),
    )

    decisions = task_store.latest_manager_decisions(root)

    assert {task_id: row["event"] for task_id, row in decisions.items()} == {
        "S-REJECT": "superseded"
    }


def test_routing_facts_answer_exactly_like_full_cards(repo: Path) -> None:
    cards = task_store.list_task_cards(repo, limit=5000)
    facts = task_store.list_task_routing_facts(repo, limit=5000)

    assert [fact["task_id"] for fact in facts] == [card["task_id"] for card in cards]
    for fact, card in zip(facts, cards, strict=True):
        assert cost_ledger._task_family(fact) == cost_ledger._task_family(card)
        assert cost_ledger._task_risk(fact) == cost_ledger._task_risk(card)
    assert {
        fact["task_id"]: (cost_ledger._task_family(fact), cost_ledger._task_risk(fact))
        for fact in facts
    } == _EXPECTED_FAMILY_RISK


@pytest.mark.parametrize("limit", [1, 2, 7, 21, 22, 5000, 10**9, 0, -5])
def test_routing_facts_share_the_card_reader_row_window(repo: Path, limit: int) -> None:
    facts = task_store.list_task_routing_facts(repo, limit=limit)
    cards = task_store.list_task_cards(repo, limit=limit)

    assert [fact["task_id"] for fact in facts] == [card["task_id"] for card in cards]
    assert len(facts) == min(max(limit, 1), len(_TASKS))


def test_routing_facts_carry_only_the_routing_keys(repo: Path) -> None:
    [fact] = task_store.list_task_routing_facts(repo, limit=1)

    assert set(fact) == {"task_id", "topic", "project_context", "risk_tier"}
    assert fact["task_id"] == _TASKS[-1][0]
    assert fact["project_context"] == {"task_type": "research"}
    assert fact["risk_tier"] == "medium"


def test_readers_project_no_blob_columns(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _spy_on_selected_columns(monkeypatch)

    task_store.list_task_routing_facts(repo, limit=5000)
    task_store.latest_manager_decisions(repo)

    assert not any({"card_json", "payload_json"} & set(columns) for columns in seen)
    assert ["task_id", "topic", "card_facts"] in seen
    assert ["task_id", "event", "is_reject_reason", "created_at"] in seen
