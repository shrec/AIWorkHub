"""NF-2026-02116: a cold latest-event read must not re-decode immutable history.

Measured on 0.12.89: an idle stdio server grew 130 MB -> 887 MB because the
storage janitor built a ProcessManager whose construction reconcile and GC
sweep each cold-replayed the full request projection over ~23 rotated archives
(912 MB of JSON) -- that projection exceeds the in-memory cache bound, so it was
never retained and every read decoded the whole, only-growing history.

These tests pin the two fixes: a persisted archive checkpoint (a cold read
decodes the checkpoint plus newer segments only, with answers identical to a
full replay and every invalid checkpoint ignored fail-closed), and the janitor
sweep paying ONE projection read for reconcile + GC.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

_SIBLING = Path(__file__).with_name("test_finalized_workspace_gc_b512_v1.py")
_SPEC = importlib.util.spec_from_file_location("nf02116_b512_fixtures", _SIBLING)
assert _SPEC is not None and _SPEC.loader is not None
_B512 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_B512)

process_launcher = _B512.process_launcher
worker_workspace = _B512.worker_workspace
from aiworkhub import process_event_ledger as pel  # noqa: E402
from aiworkhub import storage_retention  # noqa: E402

NOTICE = "runtime_notice"
LAUNCHER: dict[str, Any] = {
    "key_field": "request_id",
    "skip_event_kinds": (NOTICE,),
    "replace": True,
    "drop_fields": ("packet", "usage"),
}
VARIANTS: dict[str, dict[str, Any]] = {
    "merge": {"key_field": "request_id", "skip_event_kinds": (), "replace": False, "drop_fields": ()},
    "merge_drop": {
        "key_field": "request_id",
        "skip_event_kinds": (NOTICE,),
        "replace": False,
        "drop_fields": ("packet",),
    },
    "replace_skip_drop": LAUNCHER,
    "keep_fields": {
        "key_field": "request_id",
        "skip_event_kinds": (),
        "replace": False,
        "drop_fields": (),
        "keep_fields": ("request_id", "seal"),
    },
}
ARCHIVES = 6


@pytest.fixture(autouse=True)
def _cold_caches():
    pel.reset_latest_event_cache()
    yield
    pel.reset_latest_event_cache()


def _archive_name(index: int) -> str:
    return f"process_events.20260101T0000{index:02d}.000000Z.1.{index:08x}.jsonl"


def _write_rows(target: Path, rows: list[dict[str, Any]]) -> None:
    with target.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _segment_rows(index: int, *, requests: int = 8, padding: int = 64) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number in range(requests):
        request_id = f"req-{index}-{number}"
        rows.append({
            "request_id": request_id,
            "state": "starting",
            "packet": "p" * padding,
            "seal": {"attempt": number} if number % 3 == 0 else None,
        })
        rows.append({"request_id": request_id, "state": "running", "pid": 1000 + number})
        rows.append({"request_id": request_id, "event_kind": NOTICE, "state": "noise"})
        rows.append({
            "request_id": request_id,
            "state": "validation_failed" if number % 2 else "finished",
            "usage": {"tokens": number},
            "nested": {"list": [number, {"deep": True}]},
        })
    rows.append({"request_id": "req-spans", "state": f"seen-{index}", "packet": "x" * padding})
    if index <= 2:
        rows.append({"request_id": "req-archived", "state": f"archived-{index}", "seal": {"s": index}})
    rows.append({"no_request": True})
    return rows


def _build_ledger(root: Path, *, archives: int = ARCHIVES, **segment: Any) -> Path:
    directory = root / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(archives):
        target = directory / _archive_name(index)
        _write_rows(target, _segment_rows(index, **segment))
        with target.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
    path = directory / "process_events.jsonl"
    _write_rows(path, _segment_rows(99, **segment))
    return path


def _rotate(path: Path, index: int, rows: list[dict[str, Any]]) -> None:
    os.replace(path, path.with_name(_archive_name(index)))
    _write_rows(path, rows)


def _full_replay(path: Path, options: dict[str, Any]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in pel.iter_events(path):
        pel._merge_latest_row(
            latest,
            row,
            options["key_field"],
            skip_event_kinds=options["skip_event_kinds"],
            replace=options["replace"],
            drop_fields=options["drop_fields"],
            keep_fields=options.get("keep_fields", ()),
        )
    return pel.copy_projection(latest)


def _cold_read(path: Path, options: dict[str, Any]) -> dict[str, dict[str, Any]]:
    pel.reset_latest_event_cache()
    return pel.copy_projection(
        pel._latest_view(path, keep_fields=options.get("keep_fields", ()), **{
            key: value for key, value in options.items() if key != "keep_fields"
        })
    )


def _checkpoint_file(path: Path) -> Path:
    files = [item for item in pel.checkpoint_dir(path).iterdir() if item.suffix == ".json"]
    assert len(files) == 1, files
    return files[0]


def _spy_segments(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    decoded: dict[str, int] = {}
    original = pel._iter_ledger_file

    def counting(segment: Path):
        decoded.setdefault(Path(segment).name, 0)
        for row in original(segment):
            decoded[Path(segment).name] += 1
            yield row

    monkeypatch.setattr(pel, "_iter_ledger_file", counting)
    return decoded


def _segment_names(path: Path) -> set[str]:
    return {item.name for item in pel.ledger_paths(path)}


# --- golden equality ---------------------------------------------------------


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_checkpointed_cold_read_equals_full_replay(tmp_path: Path, variant: str) -> None:
    options = VARIANTS[variant]
    path = _build_ledger(tmp_path)
    expected = _full_replay(path, options)
    assert "req-archived" in expected, "a request whose latest row lives in an archive"
    if variant != "keep_fields":
        assert expected["req-archived"]["state"] == "archived-2"
        assert expected["req-spans"]["state"] == "seen-99"

    first = _cold_read(path, options)
    assert first == expected
    assert _checkpoint_file(path).is_file()

    before = pel.latest_event_checkpoint_stats()["loaded"]
    second = _cold_read(path, options)
    assert second == expected
    assert pel.latest_event_checkpoint_stats()["loaded"] == before + 1
    if "keep_fields" not in options:
        pel.reset_latest_event_cache()
        assert pel.latest_events(path, **options) == expected


def test_checkpoint_survives_further_rotations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _build_ledger(tmp_path)
    _cold_read(path, LAUNCHER)
    decoded = _spy_segments(monkeypatch)
    for index in range(ARCHIVES, ARCHIVES + 3):
        _rotate(path, index, _segment_rows(100 + index))
        expected = _full_replay(path, LAUNCHER)
        decoded.clear()
        assert _cold_read(path, LAUNCHER) == expected
        # Only the archive rotated since the checkpoint plus the active file.
        assert set(decoded) == {_archive_name(index), path.name}


# --- bounded decode work -------------------------------------------------------


def test_cold_read_decodes_only_checkpoint_and_newer_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _build_ledger(tmp_path)
    expected = _full_replay(path, LAUNCHER)
    decoded = _spy_segments(monkeypatch)

    assert _cold_read(path, LAUNCHER) == expected
    assert set(decoded) == _segment_names(path), "first cold read is the full replay"
    active_rows = len(_segment_rows(99))

    decoded.clear()
    assert _cold_read(path, LAUNCHER) == expected
    assert decoded == {path.name: active_rows}


def test_janitor_style_projection_is_not_decoded_twice_after_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly the launcher's projection (replace, notice skip, drop fields)."""

    path = _build_ledger(tmp_path)
    options = {
        "key_field": "request_id",
        "skip_event_kinds": (
            process_launcher.RUNTIME_NOTICE_EVENT_KIND,
            *sorted(process_launcher.ORCHESTRATOR_ADVISORY_EVENT_TYPES),
        ),
        "replace": True,
        "drop_fields": process_launcher._LATEST_BY_REQUEST_DROP_FIELDS,
    }
    expected = _full_replay(path, options)
    assert pel.latest_events(path, **options) == expected
    decoded = _spy_segments(monkeypatch)
    for _ in range(2):
        pel.reset_latest_event_cache()
        assert pel.latest_events(path, **options) == expected
    assert set(decoded) == {path.name}


def _measure(path: Path) -> int:
    """tracemalloc peak of one cold launcher-projection read."""

    pel.reset_latest_event_cache()
    tracemalloc.start()
    try:
        pel._latest_view(path, keep_fields=(), **LAUNCHER)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


def test_cold_read_peak_is_bounded_by_checkpoint_not_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scaled-down measurement: the archived history doubles, the projection
    does not, and the checkpointed cold read decodes only the active rows with
    a tracemalloc peak that does not grow with the history."""

    results: dict[int, dict[str, int]] = {}
    for archives in (ARCHIVES, 2 * ARCHIVES):
        root = tmp_path / f"a{archives}"
        path = _build_ledger(root, archives=archives, requests=4, padding=16384)
        # Same request ids in every archive: history grows, projection does not.
        for index in range(archives):
            target = path.with_name(_archive_name(index))
            rows = [
                {
                    **row,
                    "request_id": str(row.get("request_id") or "").replace(
                        f"req-{index}-", "req-x-"
                    ),
                }
                for row in pel._iter_ledger_file(target)
            ]
            target.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        history = sum(path.with_name(_archive_name(i)).stat().st_size for i in range(archives))
        full_peak = _measure(path)  # full replay; publishes the checkpoint
        decoded = _spy_segments(monkeypatch)
        checkpoint_peak = _measure(path)
        monkeypatch.undo()
        results[archives] = {
            "history_bytes": history,
            "active_bytes": path.stat().st_size,
            "checkpoint_bytes": _checkpoint_file(path).stat().st_size,
            # Header + one line per projected key + trailer.
            "checkpoint_rows": len(_checkpoint_file(path).read_bytes().splitlines()) - 2,
            "full_peak": full_peak,
            "checkpoint_peak": checkpoint_peak,
            "decoded_segments": len(decoded),
        }
        assert decoded == {path.name: len(_segment_rows(99, requests=4))}

    small, large = results[ARCHIVES], results[2 * ARCHIVES]
    assert large["history_bytes"] > 1.8 * small["history_bytes"], results
    assert large["checkpoint_rows"] == small["checkpoint_rows"], results
    # Bounded by checkpoint + active segment, not by history.
    assert large["checkpoint_peak"] <= small["checkpoint_peak"] * 1.25 + 64 * 1024, results
    assert large["checkpoint_peak"] < large["history_bytes"], results


# --- fail-closed checkpoint validation ----------------------------------------


def _rewrite_header(target: Path, **changes: Any) -> None:
    header, rest = target.read_bytes().split(b"\n", 1)
    data = json.loads(header)
    data.update(changes)
    target.write_bytes(json.dumps(data, sort_keys=True).encode("ascii") + b"\n" + rest)


def _corrupt(case: str, path: Path) -> None:
    target = _checkpoint_file(path)
    if case == "missing":
        target.unlink()
    elif case == "truncated":
        data = target.read_bytes()
        target.write_bytes(data[: len(data) // 2])
    elif case == "flipped_payload":
        header, rest = target.read_bytes().split(b"\n", 1)
        assert b'"finished"' in rest
        target.write_bytes(header + b"\n" + rest.replace(b'"finished"', b'"finishex"', 1))
    elif case == "rule_version":
        _rewrite_header(target, rule_version=pel._CHECKPOINT_RULE_VERSION + 1)
    elif case == "schema":
        _rewrite_header(target, schema_id="aiworkhub.process_event_checkpoint.v0")
    elif case == "identity":
        _rewrite_header(target, identity="[\"other\"]")
    elif case == "archive_changed":
        _write_rows(path.with_name(_archive_name(2)), [{"request_id": "req-archived", "state": "late"}])
    elif case == "archive_removed":
        path.with_name(_archive_name(0)).unlink()
    elif case.startswith("deep_nesting_"):
        # json raises RecursionError, not a ValueError, on deep nesting.
        lines = target.read_bytes().splitlines(keepends=True)
        index = {"deep_nesting_header": 0, "deep_nesting_payload": 1, "deep_nesting_trailer": -1}[case]
        lines[index] = b"[" * 200_000 + b"\n"
        target.write_bytes(b"".join(lines))
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "truncated",
        "flipped_payload",
        "rule_version",
        "schema",
        "identity",
        "archive_changed",
        "archive_removed",
        "deep_nesting_header",
        "deep_nesting_payload",
        "deep_nesting_trailer",
    ],
)
def test_invalid_checkpoint_falls_back_to_full_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    path = _build_ledger(tmp_path)
    _cold_read(path, LAUNCHER)
    _corrupt(case, path)
    expected = _full_replay(path, LAUNCHER)
    if case == "archive_changed":
        assert expected["req-archived"]["state"] == "late"

    decoded = _spy_segments(monkeypatch)
    assert _cold_read(path, LAUNCHER) == expected
    assert set(decoded) == _segment_names(path), "fail-closed: full replay"

    # The fallback republished a valid checkpoint for the current archives.
    decoded.clear()
    assert _cold_read(path, LAUNCHER) == expected
    assert set(decoded) == {path.name}


def test_checkpoint_older_than_its_archive_is_stale(tmp_path: Path) -> None:
    path = _build_ledger(tmp_path)
    _cold_read(path, LAUNCHER)
    target = _checkpoint_file(path)
    archives, _active = pel._checkpoint_segments(path, pel._ledger_signatures(path))
    identity = pel._checkpoint_identity(path, pel._projection_key(path, keep_fields=(), **LAUNCHER))
    assert pel._decode_checkpoint(target, identity, archives, target.stat().st_mtime_ns) is not None
    assert pel._decode_checkpoint(target, identity, archives, 0) is None


def test_unstable_read_neither_caches_nor_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _build_ledger(tmp_path)
    expected = _full_replay(path, LAUNCHER)
    original = pel._ledger_signatures
    calls = {"n": 0}

    def rotating(target: Path):
        calls["n"] += 1
        signatures = original(target)
        # current, before: real; after: a concurrent rotation landed.
        if calls["n"] >= 3 and signatures is not None:
            return (*signatures, ("concurrent", 0, 0, 0, 0, 0))
        return signatures

    monkeypatch.setattr(pel, "_ledger_signatures", rotating)
    assert _cold_read(path, LAUNCHER) == expected
    assert not pel.checkpoint_dir(path).exists()
    assert pel.latest_event_cache_stats()["entries"] == 0


def test_spill_disables_checkpoint(tmp_path: Path) -> None:
    path = _build_ledger(tmp_path)
    spill = path.with_name("process_events.spill.20260101T000500.000000Z.1.abc.jsonl")
    _write_rows(spill, [{"request_id": "req-spans", "state": "spilled", "timestamp": "2026"}])
    assert _cold_read(path, LAUNCHER) == _full_replay(path, LAUNCHER)
    assert not pel.checkpoint_dir(path).exists()


def test_checkpoint_directory_is_never_a_ledger_segment(tmp_path: Path) -> None:
    path = _build_ledger(tmp_path)
    _cold_read(path, LAUNCHER)
    assert pel.checkpoint_dir(path).is_dir()
    assert all(item.parent == path.parent and item.suffix == ".jsonl" for item in pel.ledger_paths(path))
    assert pel.checkpoint_dir(path).name not in _segment_names(path)


# --- janitor: ONE projection read per sweep -----------------------------------


def _launcher_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    reads: list[int] = []
    original = pel.latest_events

    def counting(*args: Any, **kwargs: Any):
        if kwargs.get("drop_fields") == process_launcher._LATEST_BY_REQUEST_DROP_FIELDS:
            reads.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(pel, "latest_events", counting)
    return reads


def _janitor_manager_factory(tmp_path: Path, card: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    real = process_launcher.ProcessManager
    built = _B512._build_manager(tmp_path, card)

    def factory(*, repo: Path) -> Any:  # noqa: ARG001 - storage_retention passes the root
        return real(
            repo=built.repo,
            process_log_path=built.process_log_path,
            process_dir=built.process_dir,
            show_task=built._show_task,
            collision_guard=built._collision_guard,
            adapter_builder=built._adapter_builder,
            isolation_enabled=True,
        )

    monkeypatch.setattr(process_launcher, "ProcessManager", factory)
    return built


def _seed_terminal(manager: Any, count: int) -> None:
    for number in range(count):
        manager._append_event({
            "request_id": f"req-terminal-{number}",
            "task_id": "TASK_B512",
            "state": "finished",
            "workspace_retained": False,
        })


def test_janitor_worktrees_step_reads_the_projection_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = _B512._card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    built = _janitor_manager_factory(tmp_path, card, monkeypatch)
    _seed_terminal(built, 5)
    reads = _launcher_reads(monkeypatch)

    result = storage_retention._janitor_worktrees(tmp_path)

    assert reads == [1], "construction reconcile + GC share one projection read"
    assert result == {"gc_scanned": 0, "gc_cleaned": 0, "gc_skipped": 0}

    # Unscoped (every other caller): unchanged, one read per call.
    reads.clear()
    manager = process_launcher.ProcessManager(repo=tmp_path)
    assert manager._gc_finalized_workspaces() == result
    assert len(reads) == 2


def test_janitor_gc_decision_is_unchanged_under_shared_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "wtroot"))
    card = _B512._card()
    card.update({"status": "finished", "worker_status": "done", "claimed_by": ""})
    built = _janitor_manager_factory(tmp_path, card, monkeypatch)
    path, home, _ = _B512._seed_gc_candidate(
        built, tmp_path, card,
        request_id="req-nf02116-dead",
        pid=2_147_483_051, pid_start_ticks=999_999_901,
        state="validation_failed",
    )

    assert storage_retention._janitor_worktrees(tmp_path) == {
        "gc_scanned": 1, "gc_cleaned": 1, "gc_skipped": 0,
    }
    assert not path.exists() and not home.exists()
    # The GC append moved the generation: the next sweep reads afresh and
    # sees the candidate is no longer retained.
    assert storage_retention._janitor_worktrees(tmp_path) == {
        "gc_scanned": 0, "gc_cleaned": 0, "gc_skipped": 0,
    }


def test_shared_projection_is_generation_bound_and_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = _B512._card()
    manager = _B512._build_manager(tmp_path, card)
    _seed_terminal(manager, 2)
    reads = _launcher_reads(monkeypatch)
    with process_launcher.shared_request_projection():
        first = manager._latest_by_request()
        first["req-terminal-0"]["state"] = "mutated-by-caller"
        second = manager._latest_by_request()
        assert reads == [1]
        assert second["req-terminal-0"]["state"] == "finished", "callers get private copies"
        manager._append_event({"request_id": "req-terminal-0", "task_id": "TASK_B512", "state": "archived"})
        third = manager._latest_by_request()
        assert third["req-terminal-0"]["state"] == "archived"
        assert reads == [1, 1]
    manager._latest_by_request()
    manager._latest_by_request()
    assert len(reads) == 4, "outside the scope nothing is reused"


def test_orphaned_checkpoint_temporaries_are_swept(tmp_path: Path) -> None:
    path = _build_ledger(tmp_path)
    _cold_read(path, LAUNCHER)
    target = _checkpoint_file(path)
    orphan = target.parent / f".{target.stem}.1.dead.tmp"
    live = target.parent / f".{target.stem}.2.live.tmp"
    orphan.write_bytes(b"x")
    live.write_bytes(b"y")
    os.utime(orphan, (0, 0))
    target.unlink()
    _cold_read(path, LAUNCHER)  # republishes, sweeping orphans first
    assert not orphan.exists()
    assert live.exists(), "a temporary younger than an hour may be a live write"
    _checkpoint_file(path)
