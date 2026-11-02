"""NF-2026-01963: Source Graph body/file reads of a stale file serve the disk.

Field report: right after an accept, while the Source Graph writer was busy
(status ``stale``, ``refreshable: false``), file/body queries on the promoted
files returned pre-accept content or nothing, and nothing said why a refresh
could not be forced.

Pinned here:
  * a file changed on disk with no re-index answers ``file`` (START-END) with
    exactly the disk lines and ``body`` with the new function body and its new
    line range, both labelled ``stale_served_from_disk``;
  * the indexed generation's line numbers are never used to slice the changed
    bytes, and a file deleted on disk still reports ``missing`` with no source;
  * a health / refresh reply whose ``refreshable`` is false names why.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from aiworkhub import source_graph as sg
from aiworkhub import source_graph_daemon as sgd
from aiworkhub.repository_state import bootstrap_repository


OLD = (
    "def keep():\n"
    "    return 0\n"
    "\n"
    "def target(value):\n"
    "    return value + 1\n"
    "\n"
    "def tail():\n"
    "    return 2\n"
)

# Six new lines ahead of ``target`` and a longer body: the indexed range (4-5)
# now points at comment lines, and the real body sits at 10-13.
NEW = (
    "def keep():\n"
    "    return 0\n"
    "\n"
    "# accepted change: a leading comment block\n"
    "# second line\n"
    "# third line\n"
    "# fourth line\n"
    "# fifth line\n"
    "# sixth line\n"
    "def target(value):\n"
    "    doubled = value * 2\n"
    "    shifted = doubled + 99\n"
    "    return shifted\n"
    "\n"
    "def tail():\n"
    "    return 2\n"
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stale_repo(tmp_path: Path) -> Path:
    root = tmp_path / "stale_reads"
    root.mkdir()
    bootstrap_repository(root, repo_name="stale_reads")
    (root / "pkg").mkdir()
    (root / "pkg" / "svc.py").write_bytes(OLD.encode("utf-8"))
    sg.build_index(root, incremental=False)
    # Mutate on disk WITHOUT re-indexing: the indexed rows are now stale.
    (root / "pkg" / "svc.py").write_bytes(NEW.encode("utf-8"))
    return root


def test_ranged_file_read_of_stale_file_returns_exact_disk_window(tmp_path):
    repo = _stale_repo(tmp_path)
    db_path = sg.resolve_db_path(repo)
    generation = db_path.stat().st_mtime_ns

    payload = sg.file_query(repo, "pkg/svc.py", 64, line_range=(9, 13))

    context = payload["contexts"][0]
    assert context["source_preview"].splitlines() == NEW.splitlines()[8:13]
    assert context["source_preview_line_start"] == 9
    assert context["source_preview_line_end"] == 13
    assert context["freshness"]["state"] == sg.STALE_SERVED_FROM_DISK
    assert context["freshness"]["served"] == sg.STALE_SERVED_FROM_DISK
    assert context["freshness"]["disk_source_hash"] == _sha256(NEW)
    assert context["freshness"]["indexed_source_hash"] == _sha256(OLD)
    assert payload["freshness"] == sg.STALE_SERVED_FROM_DISK
    # A read never rebuilds the index.
    assert db_path.stat().st_mtime_ns == generation


def test_body_read_of_stale_file_returns_new_body_and_new_range(tmp_path):
    repo = _stale_repo(tmp_path)
    db_path = sg.resolve_db_path(repo)
    generation = db_path.stat().st_mtime_ns

    payload = sg.body_query(repo, "target")

    match = payload["matches"][0]
    assert (match["line_start"], match["line_end"]) == (10, 13)
    assert match["source"] == "\n".join(NEW.splitlines()[9:13])
    assert "value + 1" not in match["source"]
    assert match["freshness"]["state"] == "stale"
    assert match["freshness"]["served"] == sg.STALE_SERVED_FROM_DISK
    assert match["freshness"]["served_source_hash"] == _sha256(NEW)
    assert db_path.stat().st_mtime_ns == generation


def test_function_and_class_reads_of_stale_file_use_the_current_range(tmp_path):
    repo = _stale_repo(tmp_path)

    func = sg.function_query(repo, "target")["matches"][0]
    assert (func["line_start"], func["line_end"]) == (10, 13)
    assert func["source"].startswith("def target(value):\n    doubled")
    assert func["freshness"]["served"] == sg.STALE_SERVED_FROM_DISK


def test_old_generation_lines_never_slice_changed_bytes(tmp_path):
    """A symbol that vanished from the disk file is refused, not sliced."""

    repo = _stale_repo(tmp_path)
    (repo / "pkg" / "svc.py").write_bytes(
        NEW.replace("def target(value):", "def renamed(value):").encode("utf-8")
    )

    match = sg.body_query(repo, "target")["matches"][0]
    assert match["freshness"]["state"] == "stale"
    assert "served" not in match["freshness"]
    assert match["source"] == ""
    # The indexed range (4-5) is reported as-is, never used on the new bytes.
    assert (match["line_start"], match["line_end"]) == (4, 5)


def test_deleted_file_still_reports_missing_with_no_source(tmp_path):
    repo = _stale_repo(tmp_path)
    (repo / "pkg" / "svc.py").unlink()

    body = sg.body_query(repo, "target")
    assert body["freshness"] == "missing"
    assert body["matches"][0]["freshness"]["state"] == "missing"
    assert body["matches"][0]["source"] == ""

    func = sg.function_query(repo, "target")
    assert func["matches"][0]["freshness"]["state"] == "missing"
    assert func["matches"][0]["source"] == ""

    ranged = sg.file_query(repo, "pkg/svc.py", 64, line_range=(1, 5))
    assert ranged["freshness"] == "missing"
    for context in ranged["contexts"]:
        assert context["freshness"]["state"] == "missing"
        assert not context.get("source_preview")


def test_fresh_file_reads_carry_no_stale_label(tmp_path):
    root = tmp_path / "fresh_reads"
    root.mkdir()
    bootstrap_repository(root, repo_name="fresh_reads")
    (root / "m.py").write_bytes(OLD.encode("utf-8"))
    sg.build_index(root, incremental=False)

    ranged = sg.file_query(root, "m.py", 64, line_range=(4, 5))
    assert ranged["freshness"] == "fresh"
    assert ranged["contexts"][0]["freshness"]["state"] == "fresh"
    match = sg.body_query(root, "target")["matches"][0]
    assert "served" not in match["freshness"]
    assert (match["line_start"], match["line_end"]) == (4, 5)


# ---------------------------------------------------------------------------
# refresh_unavailable_reason
# ---------------------------------------------------------------------------

def test_unregistered_health_and_refresh_name_why_refresh_is_unavailable(tmp_path):
    root = tmp_path / "unregistered"
    root.mkdir()

    health = sgd.daemon_health(root)
    assert health["refreshable"] is False
    assert health["refresh_unavailable_reason"] == "daemon_not_registered"

    reply = sgd.refresh_now(root)
    assert reply["ok"] is False
    assert reply["refreshable"] is False
    assert reply["refresh_unavailable_reason"] == "daemon_not_registered"


def _payload(**overrides):
    base = {
        "ok": False,
        "status": sgd.STATUS_STALE,
        "running": True,
        "registered": True,
        "writer_state": "active",
        "last_error": "",
        "refreshable": False,
        "refresh_job": None,
        "recovery": {"error": "", "phase": "", "elapsed_seconds": 0.0},
        "refresh_consumer": {"live": True, "reason": ""},
    }
    base.update(overrides)
    return base


def test_every_non_refreshable_gate_yields_a_named_reason():
    reason = sgd._refresh_unavailable_reason
    # The field-report shape: writer busy, index behind its stale threshold.
    assert reason(_payload()) == (
        "writer_behind:index_age_exceeds_stale_after:writer_active"
    )
    assert reason(_payload(refreshable=True)) == ""
    cases = [
        _payload(status=sgd.STATUS_STOPPED),
        _payload(status=sgd.STATUS_DEGRADED, last_error="boom"),
        _payload(status=sgd.STATUS_RECOVERY, recovery={"phase": "wal"}),
        _payload(status=sgd.STATUS_READY, last_error="build_start_fenced"),
        _payload(status=sgd.STATUS_READY, refresh_job={"state": "failed", "error": "x"}),
        _payload(status=sgd.STATUS_READY, refresh_consumer={"live": False, "reason": ""}),
        _payload(status=sgd.STATUS_CORRUPT, last_error="source_graph_index_corrupt:fts"),
        _payload(status=sgd.STATUS_INDEXING, last_error="unregistered_live_builder",
                 registered=False),
        _payload(status=sgd.STATUS_READY),
    ]
    reasons = [reason(case) for case in cases]
    assert all(reasons), reasons
    assert reasons[0] == "daemon_stopped"
    assert reasons[1] == "daemon_degraded:boom"
    assert reasons[2] == "recovery_in_progress:wal"
    assert reasons[3] == "build_start_fenced"
    assert reasons[4] == "last_refresh_job_failed:x"
    assert reasons[5].startswith("no_live_writer:")
    assert reasons[6].startswith("index_corrupt:")
    assert reasons[7] == "writer_active:unregistered_live_builder"
