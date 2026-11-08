"""NF-2026-01931: the janitor reclaims Source Graph base pins and overlays.

A terminal request's runtime home keeps a ~1 GB ``.sg-base-pin.*.sqlite`` and
a ``candidate_source_graph_*.sqlite`` overlay after its process exits. The
``base_pins`` janitor step deletes them -- but never while anything can still
read them -- and the pin receipt names why ``_link_base_pin`` fell back to a
full copy instead of a hard link.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

from aiworkhub import process_launcher, storage_retention
from aiworkhub import source_graph_partition as sgp

_PIN = ".sg-base-pin.64769.1234.1048576.1700000000000000000.sqlite"
_OVERLAY = "candidate_source_graph_0123456789abcdef.sqlite"
_TERMINAL = frozenset({"finished", "rejected", "blocked", "review_ready"})


def _seed_home(home: Path) -> int:
    """Write one pin + overlay (with WAL/SHM) and an unrelated file; return pin bytes."""

    runtime = home / "task_mcp_worker_runtime"
    runtime.mkdir(parents=True)
    total = 0
    for name, size in (
        (_PIN, 4096),
        (_PIN + "-wal", 512),
        (_PIN + "-shm", 256),
        (_OVERLAY, 2048),
        (_OVERLAY + "-wal", 128),
    ):
        (runtime / name).write_bytes(b"x" * size)
        total += size
    (runtime / "keep.sqlite").write_bytes(b"k" * 64)
    (home / "notes.txt").write_text("keep", encoding="utf-8")
    return total


def _artifacts(home: Path) -> list[str]:
    return sorted(
        p.name for p in home.rglob("*")
        if storage_retention._is_base_pin_artifact(p.name)
    )


def _run(
    latest: Mapping[str, Mapping[str, Any]],
    homes: Mapping[str, Path],
    *,
    active: set[str] | None = None,
    protected: Mapping[str, str] | None = None,
    verified: bool = True,
    alive: set[str] | None = None,
    reviewing: set[str] | None = None,
) -> dict[str, Any]:
    alive = alive or set()
    reviewing = reviewing or set()
    return storage_retention._reclaim_terminal_base_pins(
        latest,
        terminal_states=_TERMINAL,
        active=active or set(),
        protected=protected or {},
        verified=verified,
        process_dead=lambda event: str(event["rid"]) not in alive,
        reviewers_running=lambda _task, rid: rid in reviewing,
        home_of=lambda rid, _event: homes.get(rid),
    )


def _event(rid: str, state: str) -> dict[str, Any]:
    return {"rid": rid, "state": state, "task_id": f"T-{rid}"}


def test_terminal_requests_lose_pins_and_overlays_with_reclaimed_bytes(tmp_path: Path) -> None:
    homes: dict[str, Path] = {}
    expected = 0
    latest = {}
    for rid, state in (("fin", "finished"), ("rej", "rejected"), ("blk", "blocked")):
        homes[rid] = tmp_path / rid
        expected += _seed_home(homes[rid])
        latest[rid] = _event(rid, state)

    result = _run(latest, homes)

    assert result["ok"] is True, result
    assert result["bytes"] == expected
    assert result["removed"] == 15
    assert result["reclaimed_requests"] == 3
    assert result["skipped"] == []
    for home in homes.values():
        assert _artifacts(home) == []
        assert (home / "task_mcp_worker_runtime" / "keep.sqlite").is_file()
        assert (home / "notes.txt").is_file()


def test_still_read_requests_keep_pins_and_each_skip_is_named(tmp_path: Path) -> None:
    cases = {
        "ready": ("review_ready", "review_ready"),
        "child": ("finished", "reviewer_running"),
        "pred": ("rejected", "rework_predecessor_retained"),
        "alive": ("finished", "process_alive"),
        "active": ("blocked", "process_alive"),
        "running": ("running", "process_not_terminal:running"),
    }
    homes = {rid: tmp_path / rid for rid in cases}
    for home in homes.values():
        _seed_home(home)
    latest = {rid: _event(rid, state) for rid, (state, _reason) in cases.items()}

    result = _run(
        latest,
        homes,
        active={"active"},
        protected={"pred": "rework_predecessor_retained"},
        alive={"alive"},
        reviewing={"child"},
    )

    assert result["ok"] is True
    assert result["removed"] == 0 and result["bytes"] == 0
    reasons = {row["request_id"]: row["reason"] for row in result["skipped"]}
    assert reasons == {rid: reason for rid, (_state, reason) in cases.items()}
    for home in homes.values():
        assert len(_artifacts(home)) == 5


def test_unknown_reviewer_liveness_or_unverified_protection_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / "fin"
    _seed_home(home)
    latest = {"fin": _event("fin", "finished")}

    unverified = _run(latest, {"fin": home}, verified=False)
    assert unverified["refused"] == "protection_unverified"
    assert unverified["skipped"] == [{"request_id": "fin", "reason": "protection_unverified"}]

    def broken(_task: str, _rid: str) -> bool:
        raise RuntimeError("ledger unreadable")

    unknown = storage_retention._reclaim_terminal_base_pins(
        latest, terminal_states=_TERMINAL, active=set(), protected={}, verified=True,
        process_dead=lambda _e: True, reviewers_running=broken,
        home_of=lambda _r, _e: home,
    )
    assert unknown["skipped"] == [{"request_id": "fin", "reason": "reviewer_state_unknown"}]
    assert len(_artifacts(home)) == 5


def test_janitor_step_wires_existing_protection_decisions(tmp_path: Path, monkeypatch) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    log = tmp_path / "process.jsonl"
    log.write_text("", encoding="utf-8")
    latest: dict[str, dict[str, Any]] = {}
    homes: dict[str, Path] = {}
    for rid, state in (("done", "finished"), ("ready", "review_ready"), ("pred", "rejected")):
        home = tmp_path / "homes" / rid
        _seed_home(home)
        homes[rid] = home
        meta = tmp_path / f"{rid}.meta.json"
        meta.write_text(json.dumps({"workspace": {
            "repo": str(repo), "path": str(tmp_path / "wt" / rid),
            "home": str(home), "request_id": rid,
        }}), encoding="utf-8")
        latest[rid] = {"state": state, "task_id": f"T-{rid}", "pid": 0,
                       "metadata_path": str(meta)}

    class FakeManager:
        def __init__(self, repo: Path) -> None:
            self.process_log_path = log

        def _latest_by_request(self) -> dict[str, dict[str, Any]]:
            return latest

        def _active_request_ids(self, _latest: Any) -> set[str]:
            return set()

        def _live_lens_reviewers(self, _latest: Any, _task: str, _rid: str) -> list[Any]:
            return []

    monkeypatch.setattr(process_launcher, "ProcessManager", FakeManager)
    monkeypatch.setattr(process_launcher, "_process_proven_dead", lambda _p, _t: True)
    monkeypatch.setattr(
        process_launcher, "assert_gc_safe_workspace_shape", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        storage_retention,
        "_protected_attempt_ids",
        lambda _root: ({"pred": "rework_predecessor_retained"}, True, {}, set(), set()),
    )
    monkeypatch.setattr(
        process_launcher, "GC_CANDIDATE_PROCESS_STATES",
        frozenset({"finished", "rejected", "review_ready"}),
    )

    result = storage_retention._janitor_base_pins(repo)

    assert result["ok"] is True, result
    assert result["reclaimed_requests"] == 1
    assert result["bytes"] > 0
    assert _artifacts(homes["done"]) == []
    assert len(_artifacts(homes["ready"])) == 5
    assert len(_artifacts(homes["pred"])) == 5
    assert {row["request_id"]: row["reason"] for row in result["skipped"]} == {
        "ready": "review_ready", "pred": "rework_predecessor_retained",
    }


def test_janitor_runs_base_pin_step_after_worktrees(tmp_path: Path, monkeypatch) -> None:
    order: list[str] = []
    for name in ("_janitor_worktrees", "_janitor_base_pins", "_janitor_logs",
                 "_janitor_registrations", "_janitor_records"):
        monkeypatch.setattr(
            storage_retention, name,
            lambda *_a, _n=name: order.append(_n) or {"ok": True},
        )
    monkeypatch.setattr(storage_retention, "_prune_decided_rework_deltas",
                        lambda *_a: {"ok": True})
    monkeypatch.setattr(storage_retention, "_janitor_unattributed",
                        lambda *_a: {"ok": True})
    monkeypatch.setattr(storage_retention, "_janitor_scratch", lambda *_a: {"ok": True})
    monkeypatch.setattr(storage_retention, "_clear_janitor_step_needfix",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(storage_retention, "_file_worktree_failure_needfix",
                        lambda *_a, **_k: None)
    result = storage_retention._run_repository_cleanup(tmp_path, base=tmp_path, now=0.0)
    assert order[:2] == ["_janitor_worktrees", "_janitor_base_pins"]
    assert result["base_pins"] == {"ok": True}


def _partition_helpers() -> Any:
    """The canonical base-index fixture of test_source_graph_partition (imported, not copied)."""

    path = Path(__file__).with_name("test_source_graph_partition.py")
    spec = importlib.util.spec_from_file_location("_nf01931_partition_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_exdev_copy_fallback_names_its_cause_in_the_pin_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    helpers = _partition_helpers()
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = helpers._build_base(repo, {"m.py": "def s():\n    return 1\n"})
    (repo / "m.py").write_text("def s2():\n    return 2\n", encoding="utf-8")

    def cross_device_link(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(sgp.os, "link", cross_device_link)
    partition = tmp_path / "p.sqlite"
    report = sgp.build_partition(
        repo, helpers._changed(repo, ["m.py"]), partition, base_db_path=base_db,
    )
    assert report.base_pin == "pinned"
    marker = sgp.read_composed_marker(partition)
    assert marker is not None
    pin = marker["base"]["pin"]
    assert pin["status"] == "pinned"
    assert pin["link_fallback"] == {"mode": "copy", "reason": "EXDEV"}


def test_same_volume_link_is_measured_and_any_fallback_is_named(tmp_path: Path) -> None:
    """Measure what _link_base_pin does on ONE volume (no monkeypatch).

    On a filesystem that supports hard links the same-volume link succeeds and
    no copy happens, so a field copy on "the same volume" must be a different
    device id (e.g. a junction/subst/AppContainer redirect yielding EXDEV) or a
    sandbox denial; either way the returned errno name is the recorded reason.
    """

    source = tmp_path / "base.sqlite"
    source.write_bytes(b"s" * 4096)
    observed = os.stat(source, follow_symlinks=False)
    generation = sgp._generation(observed)
    pin_path = tmp_path / sgp._pin_name(generation)
    outcome = sgp._link_base_pin(source, pin_path, generation)
    measurement = {
        "platform": sys.platform,
        "outcome": outcome or "hardlinked",
        "same_device": os.stat(tmp_path).st_dev == observed.st_dev,
    }
    print(f"nf01931_link_measurement={json.dumps(measurement, sort_keys=True)}")
    if outcome is None:
        linked = os.stat(pin_path, follow_symlinks=False)
        assert (linked.st_dev, linked.st_ino) == (observed.st_dev, observed.st_ino)
    else:
        assert outcome == "link_unsupported" or outcome in errno.errorcode.values(), outcome


@pytest.mark.parametrize(
    ("name", "matches"),
    [
        (_PIN, True),
        (_PIN + "-wal", True),
        (_PIN + "-shm", True),
        (_OVERLAY, True),
        (_OVERLAY + "-shm", True),
        ("candidate_source_graph_xyz.sqlite", False),
        ("p.sqlite", False),
        (".sg-base-pin.sqlite", False),
    ],
)
def test_artifact_recognition_is_exact(name: str, matches: bool) -> None:
    assert storage_retention._is_base_pin_artifact(name) is matches


def test_blocked_terminal_candidate_loses_pin_but_keeps_unrelated_files(
    tmp_path: Path,
) -> None:
    home = tmp_path / "blk"
    expected = _seed_home(home)

    result = _run(
        {"blk": _event("blk", "blocked")},
        {"blk": home},
        protected={"blk": "blocked_terminal_candidate_retained"},
    )

    assert result["ok"] is True, result
    assert result["reclaimed_requests"] == 1
    assert result["bytes"] == expected
    assert result["skipped"] == []
    assert _artifacts(home) == []
    assert (home / "task_mcp_worker_runtime" / "keep.sqlite").is_file()
    assert (home / "notes.txt").is_file()


def test_live_worker_and_rework_predecessor_still_skip_by_name(tmp_path: Path) -> None:
    reasons = {"live": "live_worker", "pred": "rework_predecessor_retained"}
    homes = {rid: tmp_path / rid for rid in reasons}
    for home in homes.values():
        _seed_home(home)

    result = _run(
        {rid: _event(rid, "blocked") for rid in reasons}, homes, protected=reasons,
    )

    assert result["reclaimed_requests"] == 0
    assert {row["request_id"]: row["reason"] for row in result["skipped"]} == reasons
    for home in homes.values():
        assert len(_artifacts(home)) == 5


def _overlay_janitor(
    tmp_path: Path, monkeypatch, *, log_exists: bool = True, lookup_raises: bool = False,
) -> tuple[Path, Path, dict[str, Path]]:
    """Wire ``_janitor_base_pins`` to a fake root-bound manager; return (repo, overlays, dirs)."""

    from aiworkhub import manager_ai_tools

    repo = (tmp_path / "repo").resolve()
    overlays = repo / manager_ai_tools._CANDIDATE_OVERLAY_DIR
    dirs = {name: overlays / (name * 32) for name in "ab"}
    for directory in dirs.values():
        directory.mkdir(parents=True)
        (directory / "overlay_x.sqlite").write_bytes(b"x")
    cards = {"a" * 32: ("T-A", "done", "accepted"), "b" * 32: ("T-B", "review", "review_ready")}
    log = tmp_path / "process.jsonl"
    if log_exists:
        log.write_text("", encoding="utf-8")

    class FakeManager:
        def __init__(self, repo: Path) -> None:
            self.process_log_path = log

        def _latest_by_request(self) -> dict[str, dict[str, Any]]:
            return {}

        def _active_request_ids(self, _latest: Any) -> set[str]:
            return set()

        def _request_events(self, rid: str) -> list[dict[str, str]]:
            return [{"task_id": cards[rid][0], "state": "x"}]

        def _show_task(self, task_id: str) -> dict[str, Any]:
            if lookup_raises:
                raise RuntimeError("card lookup failed")
            status, sub = next((c[1], c[2]) for c in cards.values() if c[0] == task_id)
            return {"returncode": 0, "stdout": json.dumps({
                "task_id": task_id, "status": status, "worker_status": status,
                "terminal_substatus": sub,
            })}

    monkeypatch.setattr(process_launcher, "ProcessManager", FakeManager)
    monkeypatch.setattr(
        storage_retention, "_protected_attempt_ids",
        lambda _root: ({}, True, {}, set(), set()),
    )
    return repo, overlays, dirs


def test_janitor_sweeps_finished_overlay_and_keeps_review_ready(
    tmp_path: Path, monkeypatch
) -> None:
    repo, _overlays, dirs = _overlay_janitor(tmp_path, monkeypatch)

    result = storage_retention._janitor_base_pins(repo)

    assert result["candidate_overlays_removed"] == 1
    assert not dirs["a"].exists()
    assert dirs["b"].is_dir()


def test_janitor_keeps_overlay_when_lookup_raises(tmp_path: Path, monkeypatch) -> None:
    repo, _overlays, dirs = _overlay_janitor(tmp_path, monkeypatch, lookup_raises=True)

    result = storage_retention._janitor_base_pins(repo)

    assert result["candidate_overlays_removed"] == 0
    assert all(directory.is_dir() for directory in dirs.values())


def test_janitor_touches_no_overlay_without_a_process_log(
    tmp_path: Path, monkeypatch
) -> None:
    repo, _overlays, dirs = _overlay_janitor(tmp_path, monkeypatch, log_exists=False)

    result = storage_retention._janitor_base_pins(repo)

    assert "candidate_overlays_removed" not in result
    assert all(directory.is_dir() for directory in dirs.values())


def test_overlay_sweep_failure_keeps_the_pin_reclaim_result(
    tmp_path: Path, monkeypatch
) -> None:
    from aiworkhub import manager_ai_tools

    repo, _overlays, _dirs = _overlay_janitor(tmp_path, monkeypatch)
    sentinel = {"ok": True, "reclaimed_requests": 7, "errors": []}
    monkeypatch.setattr(
        storage_retention, "_reclaim_terminal_base_pins", lambda *_a, **_k: dict(sentinel)
    )

    def explode(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(manager_ai_tools, "_sweep_candidate_overlays", explode)

    result = storage_retention._janitor_base_pins(repo)

    assert result["reclaimed_requests"] == 7 and result["ok"] is True
    assert result["candidate_overlays_removed"] == 0
    assert result["candidate_overlay_sweep_error"] == "RuntimeError"


def test_marked_partition_never_serves_rows_after_its_pin_is_reclaimed(
    tmp_path: Path,
) -> None:
    import sqlite3

    helpers = _partition_helpers()
    repo = tmp_path / "repo"
    repo.mkdir()
    base_db = helpers._build_base(repo, {"m.py": "def s():\n    return 1\n"})
    (repo / "m.py").write_text("def s2():\n    return 2\n", encoding="utf-8")
    runtime = tmp_path / "home" / "task_mcp_worker_runtime"
    runtime.mkdir(parents=True)
    overlay_named = runtime / _OVERLAY
    plain_named = runtime / "p.sqlite"
    for partition in (overlay_named, plain_named):
        sgp.build_partition(
            repo, helpers._changed(repo, ["m.py"]), partition, base_db_path=base_db,
        )
        assert sgp.read_composed_marker(partition) is not None

    outcome = storage_retention._reclaim_home_base_pins(tmp_path / "home")

    assert outcome["removed"] >= 2, outcome
    assert not [p for p in runtime.iterdir() if sgp._BASE_PIN_NAME.fullmatch(p.name)]
    assert not overlay_named.exists()
    assert plain_named.exists()
    conn = sqlite3.connect(str(plain_named))
    try:
        with pytest.raises(sgp.PartitionBaseShiftError):
            sgp.attach_composed_base_if_marked(conn, plain_named)
    finally:
        conn.close()
