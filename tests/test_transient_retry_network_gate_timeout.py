from __future__ import annotations

import concurrent.futures
import importlib
import json
import sqlite3
import sys
import threading
import time
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

autolaunch = importlib.import_module("aiworkhub.dependency_autolaunch")
task_reconciler = importlib.import_module("aiworkhub.task_reconciler")
task_store = importlib.import_module("aiworkhub.task_store")

FAILED_REQUEST_ID = "req-failed-86400"
REAL_NETWORK_REACHABLE = autolaunch.network_reachable


@pytest.fixture(autouse=True)
def _network_is_up(monkeypatch):
    monkeypatch.setattr(autolaunch, "network_reachable", lambda: True)


@pytest.fixture
def probe_threads(monkeypatch):
    # Takes monkeypatch so stragglers are joined before the socket stub is undone.
    yield
    for thread in [t for t in threading.enumerate() if t.name.startswith("network-probe")]:
        thread.join(timeout=5)
        assert not thread.is_alive()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _db(root: Path) -> Path:
    return task_store.storage_readiness(root).canonical_db


def _insert_processing_card(
    root: Path, task_id: str, *, runner: str, topic: str, launch_request_id: str
) -> None:
    """A card already claimed and running, exactly as a real launch leaves it."""
    now = "2026-10-01T00:00:00+00:00"
    card = {
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "status": "processing",
        "worker_status": "claimed",
        "origin_thread_id": f"thread_{task_id.lower()}",
        "coordinator_provider": "codex",
    }
    if launch_request_id:
        card["launch_request_id"] = launch_request_id
    conn = sqlite3.connect(_db(root))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status, "
            "claimed_by, card_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, runner, topic, "processing", "claimed", runner, json.dumps(card), now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _card(root: Path, task_id: str) -> dict:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute(
            "SELECT status, worker_status, card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    card = json.loads(row[2])
    card["_status"], card["_worker_status"] = row[0], row[1]
    return card


def _force_due(root: Path, task_id: str) -> None:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        card = json.loads(row[0])
        card["retry_not_before"] = "2000-01-01T00:00:00+00:00"
        conn.execute("UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), task_id))
        conn.commit()
    finally:
        conn.close()


def _retry_card(
    root: Path,
    task_id: str,
    *,
    request_id: str = "",
    topic: str = "task_mcp",
    due: bool = True,
) -> None:
    """Park a failed attempt as pending via the real transient-retry transition."""
    runner = f"runner_{task_id.lower()}"
    _insert_processing_card(
        root, task_id, runner=runner, topic=topic, launch_request_id=request_id
    )
    ok, state = task_store.mark_transient_retry(
        root, task_id, runner=runner, reason="provider_429", request_id=request_id
    )
    assert (ok, state) == (True, "pending")
    if due:
        _force_due(root, task_id)


def _hold_rows(root: Path, task_id: str) -> int:
    conn = sqlite3.connect(_db(root))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM dependency_autolaunch_holds WHERE task_id=?", (task_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _recording_launch(calls: list[tuple[str, str, str, str]]):
    def launch(task_id: str, runner: str, topic: str, request_id: str) -> dict:
        calls.append((task_id, runner, topic, request_id))
        return {"ok": True}

    return launch


def _probe(answer: bool, calls: list[bool]):
    def online() -> bool:
        calls.append(answer)
        return answer

    return online


def _gated_pass(root: Path, *, answer: bool):
    probes: list[bool] = []
    launches: list[tuple[str, str, str, str]] = []
    outcome = autolaunch.reconcile_transient_due(
        root, launch=_recording_launch(launches), online=_probe(answer, probes)
    )
    return outcome, probes, launches


class _Mgr:
    """The slice of ProcessManager the relaunch scan touches."""

    def __init__(self, repo: Path, records: dict | None = None, *, status_error=None):
        self.repo = repo
        self.records = records or {}
        self.status_error = status_error
        self.status_calls: list[str] = []
        self.launches: list[dict] = []

    def status(self, request_id: str) -> dict:
        self.status_calls.append(request_id)
        if self.status_error is not None:
            raise self.status_error
        return self.records.get(request_id) or {
            "ok": False,
            "request_id": request_id,
            "state": "not_found",
        }

    def launch(self, *, task_id: str, runner: str, topic: str, **extra) -> dict:
        self.launches.append({"task_id": task_id, "runner": runner, "topic": topic, **extra})
        return {"ok": True}


def _failed_record(timeout=None, *, metadata_path: Path | None = None) -> dict:
    event: dict = {"request_id": FAILED_REQUEST_ID, "task_id": "H1"}
    if timeout is not None:
        event["timeout_seconds"] = timeout
    if metadata_path is not None:
        event["metadata_path"] = str(metadata_path)
    return {"ok": True, "request_id": FAILED_REQUEST_ID, "state": "failed", "latest_event": event}


def _write_metadata(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "request-metadata.json"
    path.write_text(text, encoding="utf-8")
    return path


def _scan_with(
    tmp_path: Path, records: dict, *, request_id: str = FAILED_REQUEST_ID, status_error=None
):
    root = _init_repo(tmp_path)
    _retry_card(root, "H1", request_id=request_id)
    mgr = _Mgr(root, records, status_error=status_error)
    return mgr, task_reconciler._scan_transient_retry_relaunch(mgr)


class _FakeSocket:
    def __init__(self, network: _FakeNetwork):
        self.network = network

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def settimeout(self, seconds):
        self.network.timeouts.append(seconds)

    def connect(self, address):
        self.network.connects.append(address)
        if self.network.connect_error is not None:
            raise self.network.connect_error(f"cannot connect to {address}")
        if address[0] not in self.network.reachable:
            raise ConnectionRefusedError(address)


class _FakeNetwork:
    """Stands in for the socket module: no DNS and no real sockets."""

    SOCK_STREAM = 1

    def __init__(
        self,
        addresses: dict[str, list[str]] | None = None,
        reachable=(),
        *,
        resolve_error=None,
        connect_error=None,
        hang: threading.Event | None = None,
    ):
        self.addresses = addresses or {}
        self.reachable = set(reachable)
        self.resolve_error = resolve_error
        self.connect_error = connect_error
        self.hang = hang
        self.resolved: list[tuple[str, int]] = []
        self.connects: list[tuple[str, int]] = []
        self.timeouts: list[float] = []

    def getaddrinfo(self, host, port, *args, **kwargs):
        self.resolved.append((host, port))
        if self.hang is not None:
            self.hang.wait(timeout=30)
        if self.resolve_error is not None:
            raise self.resolve_error(f"cannot resolve {host}")
        ips = self.addresses.get(host) or []
        if not ips:
            raise OSError(f"cannot resolve {host}")
        return [(2, 1, 6, "", (ip, port)) for ip in ips]

    def socket(self, family, socktype, proto):
        return _FakeSocket(self)


# --- connectivity gate in reconcile_transient_due ---------------------------------


def test_offline_pass_delays_every_due_card_without_launch_hold_or_attempt(tmp_path):
    root = _init_repo(tmp_path)
    _retry_card(root, "G1")
    _retry_card(root, "G2")

    outcome, probes, launches = _gated_pass(root, answer=False)

    assert probes == [False]
    assert launches == []
    assert outcome["ok"] is True
    assert outcome["launched"] == []
    assert outcome["network"] == "offline"
    assert outcome["delayed"] == [
        {"task_id": "G1", "reason": "network_unreachable"},
        {"task_id": "G2", "reason": "network_unreachable"},
    ]
    for task_id in ("G1", "G2"):
        card = _card(root, task_id)
        assert card["transient_retry_attempts"] == 1
        assert card["transient_retry"]["attempts"] == 1
        assert (card["_status"], card["_worker_status"]) == ("pending", "unclaimed")
        assert _hold_rows(root, task_id) == 0


def test_online_probe_is_asked_once_and_lets_every_due_card_launch(tmp_path):
    root = _init_repo(tmp_path)
    _retry_card(root, "G3")
    _retry_card(root, "G4")

    outcome, probes, launches = _gated_pass(root, answer=True)

    assert probes == [True]
    assert launches == [
        ("G3", "runner_g3", "task_mcp", "transient-retry-autolaunch:G3:1"),
        ("G4", "runner_g4", "task_mcp", "transient-retry-autolaunch:G4:1"),
    ]
    assert [entry["task_id"] for entry in outcome["launched"]] == ["G3", "G4"]
    assert outcome["delayed"] == []
    assert "network" not in outcome


def test_without_an_online_probe_the_due_card_launches_ungated(tmp_path):
    root = _init_repo(tmp_path)
    _retry_card(root, "G5")
    launches: list[tuple[str, str, str, str]] = []

    outcome = autolaunch.reconcile_transient_due(root, launch=_recording_launch(launches))

    assert [call[0] for call in launches] == ["G5"]
    assert "network" not in outcome


def test_card_held_back_while_offline_launches_with_its_attempt_intact_once_online(tmp_path):
    root = _init_repo(tmp_path)
    _retry_card(root, "R1")
    launches: list[tuple[str, str, str, str]] = []

    offline = autolaunch.reconcile_transient_due(
        root, launch=_recording_launch(launches), online=lambda: False
    )
    online = autolaunch.reconcile_transient_due(
        root, launch=_recording_launch(launches), online=lambda: True
    )

    assert [row["reason"] for row in offline["delayed"]] == ["network_unreachable"]
    assert launches == [("R1", "runner_r1", "task_mcp", "transient-retry-autolaunch:R1:1")]
    assert [entry["attempts"] for entry in online["launched"]] == [1]


def test_offline_pass_leaves_a_card_inside_its_backoff_with_its_own_reason(tmp_path):
    root = _init_repo(tmp_path)
    _retry_card(root, "M1", due=False)
    _retry_card(root, "M2")

    outcome, probes, launches = _gated_pass(root, answer=False)

    assert probes == [False]
    assert launches == []
    assert [(row["task_id"], row["reason"]) for row in outcome["delayed"]] == [
        ("M1", "transient_retry_not_due"),
        ("M2", "network_unreachable"),
    ]


def test_probe_is_not_called_for_an_empty_queue(tmp_path):
    outcome, probes, launches = _gated_pass(_init_repo(tmp_path), answer=False)

    assert (probes, launches) == ([], [])
    assert outcome["delayed"] == []
    assert "network" not in outcome


def test_probe_is_not_called_while_the_only_card_is_inside_its_backoff(tmp_path):
    root = _init_repo(tmp_path)
    _retry_card(root, "B1", due=False)

    outcome, probes, launches = _gated_pass(root, answer=False)

    assert (probes, launches) == ([], [])
    assert [row["reason"] for row in outcome["delayed"]] == ["transient_retry_not_due"]
    assert "network" not in outcome


def test_probe_is_not_called_for_a_due_quality_review_card(tmp_path):
    root = _init_repo(tmp_path)
    _retry_card(root, "Q1", topic="quality_review")

    outcome, probes, launches = _gated_pass(root, answer=False)

    assert (probes, launches) == ([], [])
    assert [row["reason"] for row in outcome["skipped"]] == [
        "quality_review_owned_by_review_orchestrator"
    ]
    assert "network" not in outcome


@pytest.mark.parametrize("kind, bucket", [("transient", "delayed"), ("deterministic", "skipped")])
def test_probe_is_not_called_for_a_card_parked_by_an_earlier_denial(tmp_path, kind, bucket):
    root = _init_repo(tmp_path)
    _retry_card(root, "K1")
    autolaunch.reconcile_transient_due(
        root, launch=lambda *_args: {"ok": False, "stderr": "claim_conflict:task_id=K1"}
    )
    assert _hold_rows(root, "K1") == 1
    conn = sqlite3.connect(_db(root))
    try:
        conn.execute(
            "UPDATE dependency_autolaunch_holds SET kind=?, next_attempt_at=? WHERE task_id=?",
            (kind, "2999-01-01T00:00:00+00:00", "K1"),
        )
        conn.commit()
    finally:
        conn.close()

    outcome, probes, launches = _gated_pass(root, answer=False)

    assert (probes, launches) == ([], [])
    assert [row["task_id"] for row in outcome[bucket]] == ["K1"]
    assert "network" not in outcome


def test_launched_entry_copies_only_the_timeout_receipt_keys_from_the_launch_result(tmp_path):
    root = _init_repo(tmp_path)
    _retry_card(root, "R2")
    result = {
        "ok": True,
        "timeout_inherited": False,
        "timeout_inherit_reason": "timeout_not_recorded",
        "unrelated": 1,
    }

    outcome = autolaunch.reconcile_transient_due(root, launch=lambda *_args: dict(result))

    [entry] = outcome["launched"]
    assert entry["timeout_inherited"] is False
    assert entry["timeout_inherit_reason"] == "timeout_not_recorded"
    assert "unrelated" not in entry


# --- relaunch scan: gate wiring and timeout inheritance ---------------------------


def test_scan_hands_the_module_probe_to_the_reconcile_pass(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_reconcile(repo, *, launch, capacity=None, online=None):
        seen["online"] = online
        return {"ok": True, "launched": []}

    monkeypatch.setattr(autolaunch, "network_reachable", REAL_NETWORK_REACHABLE)
    monkeypatch.setattr(autolaunch, "reconcile_transient_due", fake_reconcile)

    task_reconciler._scan_transient_retry_relaunch(_Mgr(tmp_path))

    assert seen["online"] is REAL_NETWORK_REACHABLE


def test_scan_waits_without_launching_while_no_provider_is_reachable(tmp_path, monkeypatch):
    monkeypatch.setattr(autolaunch, "network_reachable", lambda: False)

    mgr, outcome = _scan_with(tmp_path, {FAILED_REQUEST_ID: _failed_record(86400)})

    assert mgr.launches == []
    assert mgr.status_calls == []
    assert outcome["launched"] == []
    assert outcome["network"] == "offline"
    assert [(row["task_id"], row["reason"]) for row in outcome["delayed"]] == [
        ("H1", "network_unreachable")
    ]


def test_scan_isolates_a_probe_that_raises_without_launching(tmp_path, monkeypatch):
    def boom() -> bool:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(autolaunch, "network_reachable", boom)

    mgr, outcome = _scan_with(tmp_path, {FAILED_REQUEST_ID: _failed_record(86400)})

    assert mgr.launches == []
    assert outcome == {"ok": False, "state": "skipped", "reason": "RuntimeError"}


def test_relaunch_keeps_the_timeout_the_failed_request_recorded(tmp_path):
    mgr, outcome = _scan_with(tmp_path, {FAILED_REQUEST_ID: _failed_record(86400)})

    assert mgr.status_calls == [FAILED_REQUEST_ID]
    assert mgr.launches == [
        {"task_id": "H1", "runner": "runner_h1", "topic": "task_mcp", "timeout_seconds": 86400}
    ]
    [launched] = outcome["launched"]
    assert launched["timeout_inherited"] is True
    assert launched["timeout_seconds"] == 86400
    assert "timeout_inherit_reason" not in launched


def test_relaunch_reads_the_timeout_from_request_metadata_when_the_event_omits_it(tmp_path):
    metadata = _write_metadata(tmp_path, json.dumps({"timeout_seconds": 86400, "timeout_enforced": False}))

    mgr, outcome = _scan_with(tmp_path, {FAILED_REQUEST_ID: _failed_record(metadata_path=metadata)})

    assert mgr.launches == [
        {"task_id": "H1", "runner": "runner_h1", "topic": "task_mcp", "timeout_seconds": 86400}
    ]
    [launched] = outcome["launched"]
    assert launched["timeout_inherited"] is True
    assert launched["timeout_seconds"] == 86400


NOT_INHERITED = [
    pytest.param(lambda tmp: {}, "", "no_failed_request_id", id="card-has-no-failed-request-id"),
    pytest.param(
        lambda tmp: {}, FAILED_REQUEST_ID, "failed_request_not_found", id="request-not-found"
    ),
    pytest.param(
        lambda tmp: {FAILED_REQUEST_ID: _failed_record()},
        FAILED_REQUEST_ID,
        "timeout_not_recorded",
        id="record-without-timeout",
    ),
    *[
        pytest.param(
            lambda tmp, value=value: {FAILED_REQUEST_ID: _failed_record(value)},
            FAILED_REQUEST_ID,
            "timeout_not_recorded",
            id=f"timeout-{value!r}",
        )
        for value in (0, -5, "86400", True)
    ],
    pytest.param(
        lambda tmp: {FAILED_REQUEST_ID: _failed_record(metadata_path=tmp / "missing.json")},
        FAILED_REQUEST_ID,
        "timeout_unreadable:FileNotFoundError",
        id="metadata-file-missing",
    ),
    pytest.param(
        lambda tmp: {
            FAILED_REQUEST_ID: _failed_record(metadata_path=_write_metadata(tmp, "{not json"))
        },
        FAILED_REQUEST_ID,
        "timeout_unreadable:JSONDecodeError",
        id="metadata-malformed-json",
    ),
    pytest.param(
        lambda tmp: {FAILED_REQUEST_ID: _failed_record(metadata_path=tmp)},
        FAILED_REQUEST_ID,
        "timeout_unreadable:not_a_regular_file",
        id="metadata-path-is-a-directory",
    ),
    pytest.param(
        lambda tmp: {
            FAILED_REQUEST_ID: _failed_record(
                metadata_path=_write_metadata(tmp, '{"timeout_enforced": false}')
            )
        },
        FAILED_REQUEST_ID,
        "timeout_not_recorded",
        id="metadata-without-timeout",
    ),
]


@pytest.mark.parametrize("records_for, request_id, reason", NOT_INHERITED)
def test_relaunch_without_a_recorded_timeout_launches_as_before_and_says_why(
    tmp_path, records_for, request_id, reason
):
    mgr, outcome = _scan_with(tmp_path, records_for(tmp_path), request_id=request_id)

    assert mgr.status_calls == ([request_id] if request_id else [])
    assert mgr.launches == [{"task_id": "H1", "runner": "runner_h1", "topic": "task_mcp"}]
    [launched] = outcome["launched"]
    assert launched["timeout_inherited"] is False
    assert launched["timeout_inherit_reason"] == reason
    assert "timeout_seconds" not in launched


def test_relaunch_goes_ahead_without_a_timeout_when_the_status_lookup_raises(tmp_path):
    mgr, outcome = _scan_with(
        tmp_path, {}, status_error=RuntimeError("process table unavailable")
    )

    assert mgr.launches == [{"task_id": "H1", "runner": "runner_h1", "topic": "task_mcp"}]
    [launched] = outcome["launched"]
    assert launched["timeout_inherited"] is False
    assert launched["timeout_inherit_reason"] == "timeout_unreadable:RuntimeError"


def test_relaunch_works_with_a_manager_that_cannot_report_status(tmp_path):
    root = _init_repo(tmp_path)
    _retry_card(root, "H1", request_id=FAILED_REQUEST_ID)
    launches: list[tuple[str, str, str]] = []

    class _NoStatusMgr:
        repo = root

        def launch(self, *, task_id, runner, topic):
            launches.append((task_id, runner, topic))
            return {"ok": True}

    outcome = task_reconciler._scan_transient_retry_relaunch(_NoStatusMgr())

    assert launches == [("H1", "runner_h1", "task_mcp")]
    [launched] = outcome["launched"]
    assert launched["timeout_inherited"] is False
    assert launched["timeout_inherit_reason"] == "timeout_unreadable:AttributeError"


# --- network_reachable probe (socket stubbed, never a real connection) ------------


def test_probe_contract_constants():
    assert autolaunch.NETWORK_PROBE_HOSTS == ("api.anthropic.com", "api.openai.com", "api.x.ai")
    assert autolaunch.NETWORK_PROBE_PORT == 443
    assert autolaunch.NETWORK_PROBE_ATTEMPT_TIMEOUT_SECONDS == 3.0
    assert autolaunch.NETWORK_PROBE_TOTAL_TIMEOUT_SECONDS == 4.0


def test_probe_is_false_when_no_host_resolves(monkeypatch, probe_threads):
    network = _FakeNetwork()
    monkeypatch.setattr(autolaunch, "socket", network)

    assert REAL_NETWORK_REACHABLE() is False
    assert set(network.resolved) == {
        ("api.anthropic.com", 443),
        ("api.openai.com", 443),
        ("api.x.ai", 443),
    }
    assert network.connects == []


def test_probe_is_false_when_every_host_resolves_but_refuses_the_connection(
    monkeypatch, probe_threads
):
    network = _FakeNetwork({host: ["203.0.113.1"] for host in autolaunch.NETWORK_PROBE_HOSTS})
    monkeypatch.setattr(autolaunch, "socket", network)

    assert REAL_NETWORK_REACHABLE() is False
    assert network.connects == [("203.0.113.1", 443)] * len(autolaunch.NETWORK_PROBE_HOSTS)


def test_probe_is_true_when_a_single_host_accepts_the_connection(monkeypatch, probe_threads):
    network = _FakeNetwork({"api.x.ai": ["203.0.113.7"]}, reachable={"203.0.113.7"})
    monkeypatch.setattr(autolaunch, "socket", network)

    assert REAL_NETWORK_REACHABLE() is True
    assert ("203.0.113.7", 443) in network.connects
    assert set(network.timeouts) == {autolaunch.NETWORK_PROBE_ATTEMPT_TIMEOUT_SECONDS}


def test_probe_tries_the_next_address_of_a_host_after_one_is_refused(monkeypatch, probe_threads):
    network = _FakeNetwork(
        {"api.openai.com": ["198.51.100.1", "198.51.100.2"]}, reachable={"198.51.100.2"}
    )
    monkeypatch.setattr(autolaunch, "socket", network)

    assert REAL_NETWORK_REACHABLE() is True
    assert network.connects == [("198.51.100.1", 443), ("198.51.100.2", 443)]


@pytest.mark.parametrize("error", [OSError, TimeoutError, RuntimeError, UnicodeError])
def test_probe_counts_any_resolver_exception_as_failure(monkeypatch, probe_threads, error):
    monkeypatch.setattr(autolaunch, "socket", _FakeNetwork(resolve_error=error))

    assert REAL_NETWORK_REACHABLE() is False


@pytest.mark.parametrize(
    "error", [ConnectionRefusedError, TimeoutError, OSError, RuntimeError]
)
def test_probe_counts_any_connect_exception_as_failure(monkeypatch, probe_threads, error):
    network = _FakeNetwork(
        {host: ["203.0.113.1"] for host in autolaunch.NETWORK_PROBE_HOSTS}, connect_error=error
    )
    monkeypatch.setattr(autolaunch, "socket", network)

    assert REAL_NETWORK_REACHABLE() is False
    assert len(network.connects) == len(autolaunch.NETWORK_PROBE_HOSTS)


def test_probe_gives_up_within_its_total_bound_when_resolution_hangs(monkeypatch, probe_threads):
    release = threading.Event()
    monkeypatch.setattr(autolaunch, "socket", _FakeNetwork(hang=release))
    monkeypatch.setattr(autolaunch, "NETWORK_PROBE_TOTAL_TIMEOUT_SECONDS", 0.2)
    started = time.monotonic()
    try:
        result = REAL_NETWORK_REACHABLE()
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert result is False
    assert elapsed < 5


@pytest.mark.parametrize("cpus, workers", [(None, 1), (1, 1), (2, 1), (4, 3), (64, 3)])
def test_probe_pool_is_cpu_count_minus_one_capped_at_the_host_count(
    monkeypatch, probe_threads, cpus, workers
):
    created: list[int] = []
    real_executor = concurrent.futures.ThreadPoolExecutor

    def recording_executor(*args, **kwargs):
        created.append(kwargs["max_workers"])
        return real_executor(*args, **kwargs)

    monkeypatch.setattr(autolaunch, "socket", _FakeNetwork())
    monkeypatch.setattr(autolaunch, "os", types.SimpleNamespace(cpu_count=lambda: cpus))
    monkeypatch.setattr(
        autolaunch,
        "concurrent",
        types.SimpleNamespace(
            futures=types.SimpleNamespace(
                ThreadPoolExecutor=recording_executor,
                as_completed=concurrent.futures.as_completed,
            )
        ),
    )

    assert REAL_NETWORK_REACHABLE() is False
    assert created == [workers]
