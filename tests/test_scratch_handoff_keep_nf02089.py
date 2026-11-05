"""NF-2026-02089: a dead seat's ``.scratch/<session_id>`` outlives what names it.

The dead-seat janitor (NF-2026-02029) used to reclaim a dead manager seat's
scratch 2h after its last write, deleting tools that the seat's own handoff
named as the place they live.  Now:

* the scratch is kept while any manager-loop handoff / session record, or any
  Session Manager, AI Memory or KB row, names its session id or path;
* an unreferenced dead seat is reclaimed only past a 72h grace, overridable
  from ``retention.scratch_dead_seat_grace_hours`` in the repo policy;
* every removal (and every referenced seat kept) is reported in the sweep.

Process liveness is monkeypatched and ages are injected because utime is
denied in the worker sandbox.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from aiworkhub import runtime_temp, storage_retention  # noqa: E402

_HOUR = 3600.0
_SESSION_ID = "seat-nf02089-old"


def _repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    (root / ".aiworkhub").mkdir(parents=True)
    return root


def _dead_seat(root: Path, monkeypatch: pytest.MonkeyPatch, session_id: str = _SESSION_ID) -> Path:
    manifest = runtime_temp.write_seat_stamp(root, session_id)
    assert manifest is not None
    seat = root / ".scratch" / session_id
    (seat / "tools").mkdir()
    (seat / "tools" / "probe.py").write_text("print('probe')\n", encoding="utf-8")
    monkeypatch.setattr(runtime_temp, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(runtime_temp, "process_start_ticks", lambda pid: None)
    return seat


def _sweep(
    root: Path, monkeypatch: pytest.MonkeyPatch, ages: dict[Path, float]
) -> dict[str, object]:
    now = time.time()
    monkeypatch.setattr(
        storage_retention, "_janitor_mtime", lambda path: now - ages.get(path, 0.0)
    )
    return storage_retention._janitor_scratch(root, now)


def _write_loop_file(root: Path, dirname: str, name: str, text: str) -> Path:
    directory = root / ".aiworkhub" / "runtime" / "manager_loop" / dirname
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def _write_store(root: Path, relative: str, text: str) -> Path:
    path = root / ".aiworkhub" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE rows(id INTEGER PRIMARY KEY, key TEXT, value TEXT)")
        con.execute("INSERT INTO rows(key, value) VALUES (?, ?)", ("note", text))
        con.commit()
    return path


def test_default_grace_is_72_hours() -> None:
    assert storage_retention._SCRATCH_DEAD_SEAT_GRACE_SECONDS == 72 * 3600


def test_dead_seat_named_by_a_handoff_is_kept_past_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    seat = _dead_seat(root, monkeypatch)
    handoff = _write_loop_file(
        root,
        "handoffs",
        "loop-session-0001.md",
        f"My tools live in .scratch/{_SESSION_ID}/tools; reuse them next session.\n",
    )

    result = _sweep(root, monkeypatch, {seat: 100 * _HOUR})

    assert result["ok"] is True, result
    assert result["removed"] == 0
    assert result["removals"] == []
    assert (seat / "tools" / "probe.py").is_file()
    (kept,) = result["kept_referenced"]
    assert kept["path"] == str(seat)
    assert kept["session_id"] == _SESSION_ID
    assert kept["references"] == [f"handoff:{handoff.name}"]


def test_dead_seat_named_by_a_session_record_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    seat = _dead_seat(root, monkeypatch)
    _write_loop_file(
        root, "sessions", "mls-0001.json", json.dumps({"provider_session": _SESSION_ID})
    )

    result = _sweep(root, monkeypatch, {seat: 100 * _HOUR})

    assert result["removed"] == 0
    assert seat.is_dir()
    assert result["kept_referenced"][0]["references"] == ["session_record:mls-0001.json"]


@pytest.mark.parametrize(
    ("relative", "label"),
    [("memory/memory.sqlite", "memory"), ("kb/knowledge.sqlite", "kb")],
)
def test_dead_seat_named_by_a_store_row_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str, label: str
) -> None:
    root = _repo(tmp_path)
    seat = _dead_seat(root, monkeypatch)
    _write_store(root, relative, f"checkpoint: scratch tools in .scratch/{_SESSION_ID}/tools")

    result = _sweep(root, monkeypatch, {seat: 100 * _HOUR})

    assert result["removed"] == 0
    assert seat.is_dir()
    assert result["kept_referenced"][0]["references"] == [f"{label}:rows"]


def _write_documents(root: Path, relative: str, kind: str, text: str) -> Path:
    path = root / ".aiworkhub" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS documents("
            "id INTEGER PRIMARY KEY, kind TEXT, content TEXT, tags TEXT)"
        )
        con.execute("INSERT INTO documents(kind, content) VALUES (?, ?)", (kind, text))
        con.commit()
    return path


@pytest.mark.parametrize("kind", ["handoff", "checkpoint"])
@pytest.mark.parametrize(
    ("relative", "label"),
    [
        ("sessions/sessions.sqlite", "session"),
        ("sessions/transcript_graph.sqlite", "transcript"),
    ],
)
def test_dead_seat_named_by_a_session_handoff_or_checkpoint_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str, label: str, kind: str
) -> None:
    root = _repo(tmp_path)
    seat = _dead_seat(root, monkeypatch)
    _write_documents(root, relative, kind, f"tools live in .scratch/{_SESSION_ID}/tools")

    result = _sweep(root, monkeypatch, {seat: 100 * _HOUR})

    assert result["removed"] == 0
    assert seat.is_dir()
    assert result["kept_referenced"][0]["references"] == [f"{label}:documents"]


def test_unreferenced_dead_seat_past_the_grace_is_removed_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    seat = _dead_seat(root, monkeypatch)
    # Records about OTHER sessions must not keep this one.
    _write_loop_file(root, "handoffs", "other.md", "tools live in .scratch/some-other-seat/\n")
    _write_store(root, "memory/memory.sqlite", "unrelated lesson")

    result = _sweep(root, monkeypatch, {seat: 100 * _HOUR})

    assert result["ok"] is True, result
    assert result["removed"] == 1
    assert not seat.exists()
    assert result["kept_referenced"] == []
    (removal,) = result["removals"]
    assert removal["path"] == str(seat)
    assert removal["session_id"] == _SESSION_ID
    assert abs(removal["age_seconds"] - 100 * 3600) <= 1
    assert removal["reason"].startswith("dead_seat_unreferenced_past_")


def test_unreferenced_dead_seat_inside_the_grace_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    seat = _dead_seat(root, monkeypatch)

    result = _sweep(root, monkeypatch, {seat: 71 * _HOUR})

    assert result["removed"] == 0
    assert result["removals"] == []
    assert seat.is_dir()


def test_policy_overrides_the_grace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path)
    seat = _dead_seat(root, monkeypatch)
    policy = root / ".aiworkhub" / "config" / "policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(
        json.dumps({"retention": {"scratch_dead_seat_grace_hours": 1}}), encoding="utf-8"
    )

    assert storage_retention._scratch_dead_seat_grace_seconds(root) == 1 * 3600
    result = _sweep(root, monkeypatch, {seat: 2 * _HOUR})

    assert result["removed"] == 1
    assert not seat.exists()


@pytest.mark.parametrize("value", [0, -5, 24 * 365 + 1, True, "6", 1.5, None])
def test_unusable_policy_value_keeps_the_default_grace(tmp_path: Path, value: object) -> None:
    root = _repo(tmp_path)
    policy = root / ".aiworkhub" / "config" / "policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(
        json.dumps({"retention": {"scratch_dead_seat_grace_hours": value}}), encoding="utf-8"
    )

    assert storage_retention._scratch_dead_seat_grace_seconds(root) == 72 * 3600


def test_unreadable_store_keeps_the_seat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path)
    seat = _dead_seat(root, monkeypatch)
    broken = root / ".aiworkhub" / "kb" / "knowledge.sqlite"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"this is not a sqlite database" * 64)

    result = _sweep(root, monkeypatch, {seat: 100 * _HOUR})

    assert result["removed"] == 0
    assert seat.is_dir()
    assert result["kept_referenced"][0]["references"] == ["reference_scan_unavailable:kb"]


def test_live_seat_is_still_never_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    seat = _dead_seat(root, monkeypatch)
    monkeypatch.setattr(runtime_temp, "_pid_alive", lambda pid: True)

    result = _sweep(root, monkeypatch, {seat: 500 * _HOUR})

    assert result["removed"] == 0
    assert seat.is_dir()


def test_aged_non_seat_removal_is_reported_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    old = root / ".scratch" / "plain-old"
    old.mkdir(parents=True)

    result = _sweep(root, monkeypatch, {old: 49 * _HOUR})

    assert result["removed"] == 1
    (removal,) = result["removals"]
    assert removal["path"] == str(old)
    # Aged non-seat entries (caches, artifacts, plain dirs) are not sessions.
    assert removal["session_id"] is None
    assert removal["reason"].startswith("aged_past_")


def _write_session_record(root: Path, name: str, status: str) -> Path:
    return _write_loop_file(
        root,
        "sessions",
        name,
        json.dumps({"status": status, "notes": f"tools in .scratch/{_SESSION_ID}/tools"}),
    )


def test_seat_named_only_by_closed_record_and_non_handoff_row_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    seat = _dead_seat(root, monkeypatch)
    _write_session_record(root, "mls-closed0001.json", "closed")
    # A non-handoff row (and a non-documents table) of the session store.
    _write_store(root, "sessions/sessions.sqlite", f"misc: .scratch/{_SESSION_ID}")
    _write_documents(root, "sessions/transcript_graph.sqlite", "event", f"saw {_SESSION_ID}")

    result = _sweep(root, monkeypatch, {seat: 100 * _HOUR})

    assert result["ok"] is True, result
    assert result["kept_referenced"] == []
    assert not seat.exists()
    (removal,) = result["removals"]
    assert removal["path"] == str(seat)
    assert removal["session_id"] == _SESSION_ID


def test_seat_named_by_an_open_session_record_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    seat = _dead_seat(root, monkeypatch)
    record = _write_session_record(root, "mls-open0001.json", "open")

    result = _sweep(root, monkeypatch, {seat: 100 * _HOUR})

    assert result["removed"] == 0
    assert result["removals"] == []
    assert seat.is_dir()
    (kept,) = result["kept_referenced"]
    assert kept["references"] == [f"session_record:{record.name}"]


def test_unparseable_session_record_keeps_the_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    seat = _dead_seat(root, monkeypatch)
    _write_loop_file(root, "sessions", "mls-broken0001.json", "{not json")

    result = _sweep(root, monkeypatch, {seat: 100 * _HOUR})

    assert result["removed"] == 0
    assert seat.is_dir()
    assert result["kept_referenced"][0]["references"] == [
        "reference_scan_unavailable:session_record"
    ]


def test_deeply_nested_session_record_keeps_the_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # json.loads raises RecursionError (not ValueError) on absurd nesting.
    root = _repo(tmp_path)
    seat = _dead_seat(root, monkeypatch)
    _write_loop_file(root, "sessions", "mls-deep0001.json", "[" * 200000 + "]" * 200000)

    result = _sweep(root, monkeypatch, {seat: 100 * _HOUR})

    assert result["removed"] == 0
    assert seat.is_dir()
    assert result["kept_referenced"][0]["references"] == [
        "reference_scan_unavailable:session_record"
    ]


def test_deeply_nested_policy_file_keeps_the_default_grace(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    policy = root / ".aiworkhub" / "config" / "policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text("[" * 200000 + "]" * 200000, encoding="utf-8")

    assert storage_retention._scratch_dead_seat_grace_seconds(root) == 72 * 3600
