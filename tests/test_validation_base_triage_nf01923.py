"""NF-2026-01923: a declared validation that also fails on the base commit is environment."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    callback_store,
    core,
    launch_replay_guard,
    task_store,
    validation_auto_rework as auto,
    validation_base_triage as vbt,
    validation_failed_packet,
)
from aiworkhub.worker_workspace import ValidationRunError  # noqa: E402

_TASK = "TASK_BASE"
_REQ = "req-base-0001"
_CMD = "python -m pytest -q tests/test_x.py"
_BASE_OID = "b" * 40
_TREE = "c" * 40
_METADATA = {"task_id": _TASK, "runner": "claude", "topic": "t", "adapter_id": "claude_cli",
             "model": "claude-opus-5-5"}
_CARD = {"task_id": _TASK, "runner": "claude", "topic": "t", "validation_roles": ["reproduction"],
         "rework_predecessor": {"request_id": "old"}}


def _tail(msg: str = "assert 1 == 2", secs: str = "1.23") -> str:
    return f"FAILED tests/test_x.py::test_a - AssertionError: {msg}\n1 failed in {secs}s\n"


def _pytest_tail(*node_ids: str) -> str:
    """A real pytest short summary: FAILED lines plus the count-checked summary line."""
    lines = [f"FAILED {node_id} - AssertionError" for node_id in node_ids]
    return "\n".join(lines) + f"\n========== {len(node_ids)} failed in 0.1s ==========\n"


def _row(command: str, tail: str, returncode: int = 1) -> dict[str, Any]:
    return {"declared_command": command, "returncode": returncode, "stdout_tail": tail, "stderr_tail": ""}


def _evidence(*, base_oid: str | None = _BASE_OID) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "request_id": _REQ,
        "validation": [{"declared_command": _CMD, "returncode": 1, "stdout_tail": _tail(), "stderr_tail": ""}],
    }
    if base_oid is not None:
        evidence["request_identity"] = {"request_id": _REQ, "base_oid": base_oid}
    return evidence


@dataclasses.dataclass(frozen=True)
class _Ws:
    repo: Path
    path: Path
    home: Path
    allowed_writes: tuple = ("src/x.py",)


class _FakeLauncher:
    """The four ``process_launcher`` names the triage uses, recorded."""

    def __init__(self, repo: Path, *, returncode: Any = 1, create_raises: bool = False,
                 run_raises: bool = False) -> None:
        self.repo = repo
        self.returncode = returncode
        self.create_raises = create_raises
        self.run_raises = run_raises
        # like worker_workspace.run_validations: a failing batch RAISES with its rows
        self.raise_on_failure = False
        self.tail: Any = None
        self.created: list[dict] = []
        self.runs: list[tuple] = []
        self.cleanups: list[tuple] = []

    def create_workspace(self, repo, name, card, adapter_id, *, pinned_base_oid=None):
        self.created.append({"repo": repo, "name": name, "card": dict(card), "adapter_id": adapter_id,
                             "pinned_base_oid": pinned_base_oid})
        if self.create_raises:
            raise RuntimeError("no base workspace")
        return _Ws(repo=Path(repo), path=Path(repo) / "ws", home=Path(repo) / "home")

    def _validation_route_kwargs(self, metadata):
        return {"adapter_id": metadata["adapter_id"]}

    def _run_validations_with_toolchain_receipt(self, workspace, commands, authority, **kwargs):
        self.runs.append((workspace, tuple(commands), dict(authority), dict(kwargs)))
        if self.run_raises:
            raise OSError("sandbox died")
        rc, tail = self.returncode, self.tail
        rows = [{"declared_command": c, "returncode": rc(c) if callable(rc) else rc,
                 "stdout_tail": (tail(c) if callable(tail) else tail) if tail is not None
                 else _tail(secs="9.87"),
                 "stderr_tail": ""} for c in commands]
        if self.raise_on_failure and any(row["returncode"] != 0 for row in rows):
            raise ValidationRunError("validation_failed", rows)
        return rows

    def cleanup_workspace(self, repo, path, home):
        self.cleanups.append((repo, path, home))


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _FakeLauncher:
    fake = _FakeLauncher(tmp_path)
    monkeypatch.setattr(vbt, "_process_launcher", lambda: fake)
    monkeypatch.setattr(vbt, "base_tree", lambda repo, oid: _TREE)
    return fake


@pytest.fixture
def needfixes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """``core.needfix_add`` with the store's (title, description, scope) dedupe."""
    state: dict[str, Any] = {"rows": {}, "calls": []}

    def fake_add(title, description, scope=None, provenance=None, evidence=None, status="captured",
                 kind="other", severity="medium", tags=None, **_kw):
        state["calls"].append({"title": title, "description": description, "evidence": evidence,
                               "tags": tags, "kind": kind, "provenance": provenance})
        key = (title, description, scope)
        if key not in state["rows"]:
            state["rows"][key] = {"id": f"NF-2026-9{len(state['rows']):04d}", "status": status}
        return dict(state["rows"][key])

    monkeypatch.setattr(core, "needfix_add", fake_add)
    return state


class _Manager:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.rejects: list[tuple] = []
        self.launches: list[dict] = []

    def reject_review(self, task_id, reason, *, to="pending", predecessor_request_id=None):
        self.rejects.append((task_id, to, predecessor_request_id))
        return {"ok": True}

    def launch(self, **kwargs):
        self.launches.append(kwargs)
        return {"ok": True, "request_id": "req-next"}


@pytest.fixture
def review(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """A validation_failed card with a planned auto-return; events and wakes recorded."""
    monkeypatch.setattr(auto, "_reject_history", lambda repo, task_id: [])
    monkeypatch.setattr(launch_replay_guard, "counted_candidate_code_rejects", lambda evs, identity: [])
    monkeypatch.setattr(task_store, "get_task", lambda repo, task_id: dict(_CARD))
    evidence = _evidence()
    evidence.update(auto.plan(tmp_path, _METADATA, "validation_failed", "validation_failed:x", evidence))
    assert evidence["validation_auto_rework"]["decision"] == "auto_return"
    card = {**_CARD, "worker_status": "review", "terminal_substatus": "validation_failed",
            "terminal_review": {"request_id": _REQ, "evidence": evidence}}
    monkeypatch.setattr(task_store, "get_task", lambda repo, task_id: card)
    calls: dict[str, Any] = {"card": card, "events": [], "wakes": []}
    monkeypatch.setattr(auto, "_record_event", lambda repo, task_id, payload: calls["events"].append(payload))
    monkeypatch.setattr(auto, "_wake_manager", lambda repo, task_id, rid: calls["wakes"].append(rid) or True)
    monkeypatch.setattr(auto, "_reject_pinned", lambda manager, task_id, reason, request_id:
                        manager.reject_review(task_id, reason, to="pending", predecessor_request_id=request_id))
    return calls


_EVENT = {"state": "validation_failed", "task_id": _TASK, "request_id": _REQ, "runner": "claude", "topic": "t"}


def test_base_failed_files_one_needfix_and_never_reworks(launcher, needfixes, review, tmp_path) -> None:
    manager = _Manager(tmp_path)
    receipt = auto.after_finalize(manager, _EVENT)
    assert receipt is not None and receipt["state"] == "environment_blocked"
    assert manager.rejects == [] and manager.launches == []
    assert receipt["needfix_id"] == "NF-2026-90000"
    assert receipt["base_triage"]["outcome"] == "base_failed"
    assert review["wakes"] == [_REQ]
    [event] = review["events"]
    assert (event["state"], event["needfix_id"]) == ("environment_blocked", "NF-2026-90000")

    [call] = needfixes["calls"]
    assert call["tags"] == ["environment", "validation"]
    evidence = call["evidence"]
    assert (evidence["command"], evidence["base_oid"]) == (_CMD, _BASE_OID)
    assert (evidence["base_returncode"], evidence["candidate_returncode"]) == (1, 1)
    # both tails normalized (volatile durations stripped) and bounded
    assert "9.87" not in evidence["base_tail"] and "<dur>" in evidence["base_tail"]
    assert "1.23" not in evidence["candidate_tail"]
    for key in ("base_tail", "candidate_tail"):
        assert len(evidence[key].encode("utf-8")) <= auto._TAIL_MAX_BYTES

    # the workspace was pinned to the base, without the rework predecessor, and cleaned up
    [created] = launcher.created
    assert created["pinned_base_oid"] == _BASE_OID and created["adapter_id"] == "claude_cli"
    assert "rework_predecessor" not in created["card"]
    [run] = launcher.runs
    assert run[0].allowed_writes == () and run[1] == (_CMD,) and run[2] == {}
    assert len(launcher.cleanups) == 1


def test_second_identical_failure_dedupes_and_reuses_the_cache(launcher, needfixes, review, tmp_path) -> None:
    manager = _Manager(tmp_path)
    first = auto.after_finalize(manager, _EVENT)
    second = auto.after_finalize(manager, _EVENT)
    assert first["state"] == second["state"] == "environment_blocked"
    assert first["needfix_id"] == second["needfix_id"]
    assert len(needfixes["rows"]) == 1  # never a second NeedFix
    assert len(launcher.created) == 1  # the cache served the second decision
    assert second["base_triage"]["cache_hits"] == 1
    assert manager.rejects == [] and manager.launches == []


def test_base_passed_keeps_the_auto_return(launcher, needfixes, review, tmp_path) -> None:
    launcher.returncode = 0
    manager = _Manager(tmp_path)
    receipt = auto.after_finalize(manager, _EVENT)
    assert receipt["state"] == "relaunched"
    assert receipt["base_triage"]["outcome"] == "base_passed"
    assert "base_unrunnable_reason" not in receipt
    assert manager.rejects == [(_TASK, "pending", _REQ)] and len(manager.launches) == 1
    assert needfixes["calls"] == [] and review["wakes"] == []


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda fake: setattr(fake, "create_raises", True), "base_run_raised:RuntimeError"),
        (lambda fake: setattr(fake, "returncode", None), "receipt_returncode_missing"),
        (lambda fake: setattr(fake, "run_raises", True), "base_run_raised:OSError"),
    ],
)
def test_base_unrunnable_keeps_the_auto_return_with_the_reason(setup, reason, launcher, needfixes, review, tmp_path) -> None:
    setup(launcher)
    manager = _Manager(tmp_path)
    receipt = auto.after_finalize(manager, _EVENT)
    assert receipt["state"] == "relaunched"
    assert receipt["base_triage"]["outcome"] == "base_unrunnable"
    assert receipt["base_unrunnable_reason"] == reason
    assert len(manager.launches) == 1 and needfixes["calls"] == []
    # an unrunnable result is never cached
    assert not list(vbt.cache_dir(tmp_path).glob("*.json"))


def test_cleanup_runs_even_when_the_base_run_raises(launcher, tmp_path) -> None:
    launcher.run_raises = True
    result = vbt.triage(tmp_path, card=_CARD, base_oid=_BASE_OID, adapter_id="claude_cli", commands=[_CMD])
    assert result["outcome"] == "base_unrunnable"
    assert len(launcher.runs) == 1 and len(launcher.cleanups) == 1


def test_triage_never_raises_into_after_finalize(monkeypatch, launcher, needfixes, review, tmp_path) -> None:
    def boom(*args, **kwargs):
        raise KeyError("triage")

    monkeypatch.setattr(vbt, "triage", boom)
    manager = _Manager(tmp_path)
    receipt = auto.after_finalize(manager, _EVENT)
    assert receipt["state"] == "relaunched"
    assert receipt["base_unrunnable_reason"] == "KeyError"

    def tree_boom(repo, oid):
        raise ZeroDivisionError

    monkeypatch.undo()
    monkeypatch.setattr(vbt, "base_tree", tree_boom)
    result = vbt.triage(tmp_path, card=_CARD, base_oid=_BASE_OID, adapter_id="claude_cli", commands=[_CMD])
    assert (result["outcome"], result["reason"]) == ("base_unrunnable", "ZeroDivisionError")


def test_missing_base_oid_is_unrunnable_without_a_workspace(launcher, tmp_path) -> None:
    result = vbt.triage(tmp_path, card=_CARD, base_oid="", adapter_id="claude_cli", commands=[_CMD])
    assert (result["outcome"], result["reason"]) == ("base_unrunnable", "base_oid_missing")
    assert launcher.created == []


def test_one_command_passing_on_base_means_the_candidate_broke_it(launcher, tmp_path) -> None:
    other = "ruff check src"
    launcher.returncode = lambda command: 0 if command == other else 1
    result = vbt.triage(tmp_path, card=_CARD, base_oid=_BASE_OID, adapter_id="claude_cli", commands=[_CMD, other],
                        candidate_rows={_CMD: _row(_CMD, _tail()), other: _row(other, "src/x.py:1:1: F401")})
    assert result["outcome"] == "base_passed"
    assert [c["outcome"] for c in result["commands"]] == ["base_failed", "base_passed"]


def test_cache_lives_in_the_repo_with_a_ttl(launcher, tmp_path) -> None:
    clock = {"now": 1_000_000.0}
    directory = vbt.cache_dir(tmp_path)
    assert directory == tmp_path / ".aiworkhub" / "runtime" / "cache" / "validation_base_runs"

    def run() -> dict:
        return vbt.triage(tmp_path, card=_CARD, base_oid=_BASE_OID, adapter_id="claude_cli",
                          commands=[_CMD], now=lambda: clock["now"])

    assert run()["cache_hits"] == 0
    [entry] = list(directory.glob("*.json"))
    assert entry.name == f"{vbt.cache_key(_CMD, _TREE, 'claude_cli')}.json"
    # nothing written outside the cache directory inside the repo
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written == [entry]
    assert run()["cache_hits"] == 1 and len(launcher.created) == 1
    # a different adapter is a different key
    vbt.triage(tmp_path, card=_CARD, base_oid=_BASE_OID, adapter_id="codex_cli", commands=[_CMD],
               now=lambda: clock["now"])
    assert len(launcher.created) == 2
    # expired: deleted on read, then re-run
    clock["now"] += vbt.CACHE_TTL_SECONDS + 1
    stale = directory / f"{vbt.cache_key(_CMD, _TREE, 'claude_cli')}.json"
    assert vbt._cache_read(tmp_path, vbt.cache_key(_CMD, _TREE, "claude_cli"), clock["now"]) is None
    assert not stale.exists()
    assert run()["cache_hits"] == 0 and len(launcher.created) == 3


def test_malformed_cache_entry_is_deleted_on_read(tmp_path) -> None:
    key = vbt.cache_key(_CMD, _TREE, "claude_cli")
    path = vbt.cache_dir(tmp_path) / f"{key}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert vbt._cache_read(tmp_path, key, 0.0) is None
    assert not path.exists()


def test_cache_is_bounded(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(vbt, "CACHE_MAX_ENTRIES", 3)
    for index in range(5):
        vbt._cache_write(tmp_path, f"{index:064x}", {"outcome": "base_failed"}, 100.0 + index)
    assert len(list(vbt.cache_dir(tmp_path).glob("*.json"))) == 3


def test_base_tree_refuses_an_invalid_oid(tmp_path) -> None:
    with pytest.raises(ValueError):
        vbt.base_tree(tmp_path, "HEAD; rm -rf /")


def test_process_launcher_is_imported_lazily() -> None:
    assert "process_launcher" not in vars(vbt)
    assert "_pl" not in vars(vbt)


def test_packet_surfaces_state_and_needfix_id() -> None:
    packet = validation_failed_packet.packet_from_evidence(
        task_id=_TASK, request_id=_REQ, evidence=_evidence(), roles=None,
        auto_rework_receipt={"state": "environment_blocked", "needfix_id": "NF-2026-01999"})
    assert (packet["auto_rework_state"], packet["needfix_id"]) == ("environment_blocked", "NF-2026-01999")
    plain = validation_failed_packet.packet_from_evidence(
        task_id=_TASK, request_id=_REQ, evidence=_evidence(), roles=None)
    assert "auto_rework_state" not in plain and "needfix_id" not in plain


def test_build_packet_reads_the_receipt_for_the_same_request(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    conn = task_store._connect(task_store.canonical_db_path(repo))
    try:
        callback_store.append_event(conn, _TASK, auto.EVENT, auto.ACTOR,
                                    {"request_id": _REQ, "state": "environment_blocked", "needfix_id": "NF-2026-01999"})
    finally:
        conn.close()
    card = {"task_id": _TASK, "terminal_substatus": "validation_failed",
            "terminal_review": {"request_id": _REQ, "evidence": _evidence()}}
    packet = validation_failed_packet.build_packet(card, repo_root=repo)
    assert packet is not None
    assert (packet["auto_rework_state"], packet["needfix_id"]) == ("environment_blocked", "NF-2026-01999")
    other = {**card, "terminal_review": {"request_id": "req-other", "evidence": _evidence()}}
    assert "auto_rework_state" not in validation_failed_packet.build_packet(other, repo_root=repo)
    # an absent store is simply no state
    assert "needfix_id" not in validation_failed_packet.build_packet(card, repo_root=tmp_path / "none")


def test_needfix_identity_depends_only_on_command_and_base_tree() -> None:
    assert vbt.needfix_dedupe_identity(_CMD, _TREE) == vbt.needfix_dedupe_identity(_CMD, _TREE)
    assert vbt.needfix_dedupe_identity(_CMD, _TREE) != vbt.needfix_dedupe_identity(_CMD, "d" * 40)
    assert vbt.needfix_dedupe_identity(_CMD, _TREE) != vbt.needfix_dedupe_identity("ruff check", _TREE)


def test_long_commands_sharing_a_prefix_get_distinct_needfixes() -> None:
    prefix = "python -m pytest -q -p no:cacheprovider " + "tests/test_long_prefix_" * 10
    prefix = prefix[:200]
    assert len(prefix) == 200
    first, second = prefix + " tests/test_a.py", prefix + " tests/test_b.py"
    title_a, desc_a = vbt.needfix_dedupe_identity(first, _TREE)
    title_b, desc_b = vbt.needfix_dedupe_identity(second, _TREE)
    assert title_a != title_b and desc_a != desc_b
    assert vbt.needfix_dedupe_identity(first, _TREE)[0] == title_a
    digest = vbt.hashlib.sha256(first.encode("utf-8")).hexdigest()[:12]
    assert f"[cmd {digest}]" in title_a and f"[cmd {digest}]" in desc_a


def _triage(candidate_tail: str, **kwargs: Any) -> dict[str, Any]:
    return vbt.triage(kwargs.pop("repo"), card=_CARD, base_oid=_BASE_OID, adapter_id="claude_cli",
                      commands=[_CMD], candidate_rows={_CMD: _row(_CMD, candidate_tail)}, **kwargs)


def test_a_raised_validation_run_error_is_judged_not_unrunnable(launcher, tmp_path) -> None:
    # worker_workspace.run_validations raises ValidationRunError(reason, rows) on any failure
    launcher.raise_on_failure = True
    launcher.tail = _pytest_tail("tests/x.py::t")
    result = _triage(_pytest_tail("tests/x.py::t"), repo=tmp_path)
    assert result["outcome"] == "base_failed", result
    assert result["commands"][0]["base_returncode"] == 1
    assert len(launcher.cleanups) == 1


def test_candidate_new_failures_are_not_environment(launcher, tmp_path) -> None:
    launcher.tail = _pytest_tail("tests/x.py::a")
    result = _triage(_pytest_tail("tests/x.py::a", "tests/x.py::b"), repo=tmp_path)
    assert (result["outcome"], result["reason"]) == ("base_passed", "candidate_new_failures")
    assert result["commands"][0]["new_failures"] == ["tests/x.py::b"]


def test_candidate_failures_contained_in_base_failures_are_environment(launcher, tmp_path) -> None:
    launcher.tail = _pytest_tail("tests/x.py::a", "tests/x.py::c")
    result = _triage(_pytest_tail("tests/x.py::a"), repo=tmp_path)
    assert result["outcome"] == "base_failed"


def test_a_new_test_file_missing_on_base_is_auto_returned(launcher, needfixes, review, tmp_path) -> None:
    # the candidate added tests/test_new.py: on the base pytest exits 4, file not found
    launcher.returncode = 4
    launcher.tail = "ERROR: file or directory not found: tests/test_new.py\n\nno tests ran in 0.01s\n"
    [row] = review["card"]["terminal_review"]["evidence"]["validation"]
    row["stdout_tail"] = _pytest_tail("tests/test_new.py::t")
    manager = _Manager(tmp_path)
    receipt = auto.after_finalize(manager, _EVENT)
    assert receipt["base_triage"]["outcome"] != "base_failed"
    assert receipt["base_triage"]["outcome"] == "base_unrunnable"
    assert receipt["base_unrunnable_reason"] == "failure_identity_unparsed"
    assert receipt["state"] == "relaunched"
    assert manager.rejects == [(_TASK, "pending", _REQ)] and len(manager.launches) == 1
    assert needfixes["calls"] == []


def test_a_non_pytest_command_is_unrunnable_by_identity(launcher, tmp_path) -> None:
    command = "ruff check src"
    launcher.tail = "src/x.py:1:1: F401 unused import\nFound 1 error.\n"
    result = vbt.triage(tmp_path, card=_CARD, base_oid=_BASE_OID, adapter_id="claude_cli", commands=[command],
                        candidate_rows={command: _row(command, "src/x.py:1:1: F401 unused import\nFound 1 error.\n")})
    assert (result["outcome"], result["reason"]) == ("base_unrunnable", "failure_identity_unparsed")


def test_a_cache_hit_compares_the_stored_failure_ids(launcher, tmp_path) -> None:
    launcher.tail = _pytest_tail("tests/x.py::a")
    assert _triage(_pytest_tail("tests/x.py::a"), repo=tmp_path)["outcome"] == "base_failed"
    [entry] = list(vbt.cache_dir(tmp_path).glob("*.json"))
    stored = vbt.json.loads(entry.read_text(encoding="utf-8"))
    assert stored["base_failed_node_ids"] == ["tests/x.py::a"]
    assert stored["schema_id"] == "aiworkhub.validation_base_run_cache.v2"
    second = _triage(_pytest_tail("tests/x.py::a", "tests/x.py::b"), repo=tmp_path)
    assert second["cache_hits"] == 1 and len(launcher.created) == 1
    assert (second["outcome"], second["reason"]) == ("base_passed", "candidate_new_failures")
    assert second["commands"][0]["new_failures"] == ["tests/x.py::b"]


def test_a_v1_cache_entry_is_a_miss_and_deleted(tmp_path) -> None:
    key = vbt.cache_key(_CMD, _TREE, "claude_cli")
    path = vbt.cache_dir(tmp_path) / f"{key}.json"
    path.parent.mkdir(parents=True)
    path.write_text(vbt.json.dumps({"schema_id": "aiworkhub.validation_base_run_cache.v1", "key": key,
                                    "outcome": "base_failed", "expires_at": 10.0 ** 12}), encoding="utf-8")
    assert vbt._cache_read(tmp_path, key, 0.0) is None
    assert not path.exists()


