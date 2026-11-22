"""NF-2026-02350: cards and task events carry a tree_baseline stand-in.

The full worktree manifest stays in the request's own on-disk metadata; card
``terminal_review.evidence.workspace``, ``rework_predecessor.workspace`` and the
``terminal_review``/``accept_review`` event payloads carry only
``{summarized, entry_count, sha256}``.  Existing rows are folded by
``compact_finished_cards`` and by the write-time event bound.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import core, task_store, worker_workspace
from aiworkhub import task_store_compaction as compaction
from aiworkhub import tree_baseline_projection as projection
from aiworkhub.worker_workspace import WorkerWorkspace

REQUEST_ID = "ab" * 16
TASK_ID = "NF-2026-02350-T"
NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
OLD = "2026-10-01T00:00:00+00:00"
MEASURED_ENTRIES = 20_000
CARD_BOUND_BYTES = 64 * 1024


def _tree(count: int) -> dict[str, str]:
    return {f"src/pkg{i // 1000}/module_{i}.py": f"file:644:{i:064x}" for i in range(count)}


def _metadata(tree: dict[str, Any], *, root: Path) -> dict[str, Any]:
    return {
        "request_id": REQUEST_ID,
        "repo": str(root / "repo"),
        "path": str(root / "worktree"),
        "home": str(root / "home"),
        "allowed_writes": ["src/a.py"],
        "parent_baseline": {"src/a.py": "file:644:" + "1" * 64},
        "workspace_baseline": {"src/a.py": "file:644:" + "2" * 64},
        "tree_baseline": tree,
        "provisioning_timings_ms": {"total": 1.0},
        "inherited_rework_paths": [],
        "base_oid": "c" * 40,
        "parent_baseline_blob": {},
        "inherited_rebased_hashes": {},
        "dirty_at_seed": [],
    }


def test_projection_keeps_every_key_but_tree_baseline_exact(tmp_path: Path) -> None:
    tree = _tree(40)
    metadata = _metadata(tree, root=tmp_path)
    original = copy.deepcopy(metadata)

    projected = projection.project_workspace_metadata(metadata)

    assert metadata == original  # the input (as_metadata output) is never mutated
    stand_in = projected["tree_baseline"]
    assert projection.is_stand_in(stand_in)
    assert stand_in["entry_count"] == 40
    # One canonical digest: identical to the summary render's fold.
    rendered = core.summarize_card_baselines({"workspace": metadata})["workspace"]
    assert rendered["tree_baseline"] == stand_in
    assert {k: v for k, v in projected.items() if k != "tree_baseline"} == {
        k: v for k, v in original.items() if k != "tree_baseline"
    }
    # Idempotent, and an empty manifest folds too (no inline dict ever lands).
    assert projection.project_workspace_metadata(projected) == projected
    empty = projection.project_workspace_metadata(_metadata({}, root=tmp_path))
    assert empty["tree_baseline"]["entry_count"] == 0


def test_from_metadata_decodes_stand_in_as_no_manifest_and_legacy_unchanged(
    tmp_path: Path,
) -> None:
    tree = _tree(12)
    legacy = WorkerWorkspace.from_metadata(_metadata(dict(tree), root=tmp_path))
    assert legacy.tree_baseline == tree

    folded = projection.project_workspace_metadata(_metadata(dict(tree), root=tmp_path))
    decoded = WorkerWorkspace.from_metadata(folded)
    # Never three invented ``summarized``/``entry_count``/``sha256`` paths.
    assert decoded.tree_baseline is None
    assert decoded.workspace_baseline == folded["workspace_baseline"]
    assert decoded.parent_baseline == folded["parent_baseline"]

    workspace = WorkerWorkspace.from_metadata(_metadata(dict(tree), root=tmp_path))
    card_block = projection.card_workspace_metadata(workspace)
    assert projection.is_stand_in(card_block["tree_baseline"])
    # as_metadata() (the on-disk / in-process authority) is unchanged.
    assert workspace.as_metadata()["tree_baseline"] == tree


@pytest.fixture
def launcher_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Defaults only: neither the launcher nor the decoder sees an ambient override.
    from aiworkhub import process_launcher

    monkeypatch.delenv(process_launcher.PROCESS_DIR_ENV, raising=False)
    monkeypatch.delenv(process_launcher.PROCESS_LOG_ENV, raising=False)
    return _repo(tmp_path)


def _write_request_json(path: Path, workspace: WorkerWorkspace) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"request_id": REQUEST_ID, "task_id": TASK_ID, "workspace": workspace.as_metadata()}),
        encoding="utf-8",
    )


def test_request_metadata_path_is_the_launcher_written_path(launcher_repo: Path) -> None:
    from aiworkhub import process_launcher
    from aiworkhub.process_launcher import ProcessManager

    launcher = ProcessManager(repo=launcher_repo)
    # Same expression the launcher uses for ``metadata_path`` in _launch/launch_isolated.
    assert projection.request_metadata_path(launcher.process_dir, REQUEST_ID) == (
        launcher.process_dir / f"{REQUEST_ID}.request.json"
    )
    for unsafe in ("", ".", "..", "../x", "a/b"):
        assert projection.request_metadata_path(launcher.process_dir, unsafe) is None
    # The decoder's restated process-dir vocabulary is pinned to the launcher's.
    assert projection.PROCESS_DIR_ENV == process_launcher.PROCESS_DIR_ENV
    assert projection.PROCESS_DIR_DEFAULT_REL == process_launcher.PROCESS_DIR_DEFAULT_REL
    assert projection.default_process_dir(launcher_repo) == launcher.process_dir


def test_projection_never_imports_process_launcher() -> None:
    # The seed-closure resolver follows function-level imports too, so a
    # process_launcher import here (even lazy) would seed its whole closure into
    # every sparse validation workspace whose closure contains worker_workspace.
    import ast

    tree = ast.parse(Path(projection.__file__).read_text(encoding="utf-8"))
    local = {
        str(node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level > 0
    }
    assert local == {"request_metadata_limits"}


def test_card_built_workspace_keeps_reviewer_accept_identity(
    tmp_path: Path, launcher_repo: Path
) -> None:
    tree = _tree(25)
    workspace = WorkerWorkspace.from_metadata(_metadata(dict(tree), root=tmp_path))
    assert workspace.tree_baseline == tree
    metadata_path = projection.request_metadata_path(
        projection.default_process_dir(launcher_repo), REQUEST_ID
    )
    assert metadata_path is not None
    card_block = projection.card_workspace_metadata(workspace)
    assert projection.is_stand_in(card_block["tree_baseline"])

    # No request.json: the stand-in decodes as "no tree manifest".
    assert WorkerWorkspace.from_metadata(card_block).tree_baseline is None

    # process_launcher_accept_review.py quality-review identity check:
    # metadata_workspace.as_metadata() == card-built workspace.as_metadata().
    _write_request_json(metadata_path, workspace)
    decoded = WorkerWorkspace.from_metadata(card_block)
    assert decoded.tree_baseline == tree
    assert decoded.as_metadata() == workspace.as_metadata()

    # A stand-in whose digest differs from the on-disk map fails closed.
    tampered = dict(card_block, tree_baseline=dict(card_block["tree_baseline"], sha256="0" * 64))
    assert WorkerWorkspace.from_metadata(tampered).tree_baseline is None
    # So does an on-disk map that differs from the stand-in.
    changed = WorkerWorkspace.from_metadata(_metadata(dict(tree, extra="file:644:" + "9" * 64), root=tmp_path))
    _write_request_json(metadata_path, changed)
    assert WorkerWorkspace.from_metadata(card_block).tree_baseline is None


def test_bound_event_payload_folds_tree_baseline_without_mutating_caller(
    tmp_path: Path,
) -> None:
    full = _metadata(_tree(30), root=tmp_path)
    terminal = {"request_id": REQUEST_ID, "evidence": {"workspace": full, "small": {"ok": True}}}
    accept = {"request_id": REQUEST_ID, "workspace": full}
    before = copy.deepcopy(terminal)

    bounded = compaction.bound_event_payload("terminal_review", terminal)
    accepted = compaction.bound_event_payload("accept_review", accept)
    other = compaction.bound_event_payload("claim", {"workspace": full})

    assert terminal == before
    assert projection.is_stand_in(bounded["evidence"]["workspace"]["tree_baseline"])
    assert bounded["evidence"]["small"] == {"ok": True}
    assert bounded["evidence"]["workspace"]["workspace_baseline"] == full["workspace_baseline"]
    assert projection.is_stand_in(accepted["workspace"]["tree_baseline"])
    assert other["workspace"]["tree_baseline"] == full["tree_baseline"]


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _finished_card(full: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "status": "finished",
        "accepted_at": OLD,
        "accepted_request_id": REQUEST_ID,
        "allowed_writes": ["src/a.py"],
        "validation": ["python -m pytest -q tests/test_a.py"],
        "terminal_review": {
            "outcome": "review_ready",
            "evidence": {
                "request_identity": {"request_id": REQUEST_ID, "task_id": TASK_ID},
                "changed_path_hashes": {"src/a.py": "c" * 64},
                "claim_state": "review",
                "workspace": copy.deepcopy(full),
            },
        },
        "rework_predecessor": {
            "request_id": "cd" * 16,
            "changed_path_hashes": {"src/a.py": "d" * 64},
            "workspace": copy.deepcopy(full),
        },
    }


def test_compaction_folds_tree_baseline_of_finished_rows_under_64kb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AIWORKHUB_PROCESS_DIR", raising=False)
    repo = _repo(tmp_path)
    full = _metadata(_tree(MEASURED_ENTRIES), root=tmp_path)
    card = _finished_card(full)
    terminal_payload = {
        "request_id": REQUEST_ID,
        "evidence": {
            "request_identity": {"request_id": REQUEST_ID, "task_id": TASK_ID},
            "changed_path_hashes": {"src/a.py": "c" * 64},
            "workspace": copy.deepcopy(full),
        },
    }
    accept_payload = {"request_id": REQUEST_ID, "accepted_outcome_receipt": {"sha256": "e" * 64}}
    db = task_store.canonical_db_path(repo)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (TASK_ID, "runner", "topic", "finished", "done", json.dumps(card, sort_keys=True), OLD, OLD),
        )
        for event, payload in (("terminal_review", terminal_payload), ("accept_review", accept_payload)):
            conn.execute(
                "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) VALUES(?,?,?,?,?)",
                (TASK_ID, event, "manager", json.dumps(payload, sort_keys=True), OLD),
            )
        conn.commit()
    finally:
        conn.close()
    card_bytes_before = len(json.dumps(card, sort_keys=True).encode("utf-8"))
    payload_bytes_before = len(json.dumps(terminal_payload, sort_keys=True).encode("utf-8"))
    assert card_bytes_before > CARD_BOUND_BYTES and payload_bytes_before > CARD_BOUND_BYTES

    planned = compaction.compact_finished_cards(repo, dry_run=True, now=NOW)
    assert planned["planned"]["cards"] == 1 and planned["planned"]["events"] == 1
    # No durable attempt bundle exists: the tree fold still applies.
    report = compaction.compact_finished_cards(repo, dry_run=False, now=NOW)
    assert report["cards_compacted"] == 1 and report["events_compacted"] == 1
    assert report["skipped_not_durable"] == 1

    conn = sqlite3.connect(db)
    try:
        card_json = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (TASK_ID,)).fetchone()[0]
        rows = dict(
            conn.execute("SELECT event, payload_json FROM task_events WHERE task_id=?", (TASK_ID,)).fetchall()
        )
    finally:
        conn.close()
    payload_json = rows["terminal_review"]
    print(
        f"card_json {card_bytes_before} -> {len(card_json.encode('utf-8'))} B; "
        f"terminal_review payload_json {payload_bytes_before} -> {len(payload_json.encode('utf-8'))} B"
    )
    assert len(card_json.encode("utf-8")) < CARD_BOUND_BYTES
    assert len(payload_json.encode("utf-8")) < CARD_BOUND_BYTES
    assert json.loads(rows["accept_review"]) == accept_payload

    stored = json.loads(card_json)
    expected_stand_in = projection.baseline_stand_in(full["tree_baseline"])
    for block in (stored["terminal_review"]["evidence"]["workspace"], stored["rework_predecessor"]["workspace"]):
        assert block["tree_baseline"] == expected_stand_in
        assert {k: v for k, v in block.items() if k != "tree_baseline"} == {
            k: v for k, v in full.items() if k != "tree_baseline"
        }
    # Every other preserved evidence key stays byte-exact.
    original_evidence = card["terminal_review"]["evidence"]
    for key in ("request_identity", "changed_path_hashes", "claim_state"):
        assert stored["terminal_review"]["evidence"][key] == original_evidence[key]
    assert stored["rework_predecessor"]["changed_path_hashes"] == {"src/a.py": "d" * 64}
    event = json.loads(payload_json)
    assert event["evidence"]["workspace"]["tree_baseline"] == expected_stand_in
    assert event["evidence"]["changed_path_hashes"] == {"src/a.py": "c" * 64}

    # Idempotent: a second run has nothing left to fold.
    again = compaction.compact_finished_cards(repo, dry_run=False, now=NOW)
    assert again["cards_compacted"] == 0 and again["events_compacted"] == 0


@pytest.fixture
def retained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    authority = tmp_path / "authority"
    authority.mkdir()
    runtime = tmp_path / "runtime"
    root = runtime / "worktrees" / REQUEST_ID / "worktree"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("print('a')\n", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    process_dir = tmp_path / "processes"
    process_dir.mkdir()
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr(worker_workspace, "configured_runtime_root", lambda *_a, **_k: runtime)
    monkeypatch.setenv("AIWORKHUB_PROCESS_DIR", str(process_dir))
    full = {
        "request_id": REQUEST_ID,
        "task_id": TASK_ID,
        "path": str(root),
        "allowed_writes": ["src/a.py"],
        "workspace_baseline": {"src/a.py": worker_workspace._hash_path(root / "src" / "a.py")},
        "parent_baseline": {"src/a.py": None},
        "tree_baseline": core._bounded_worktree_manifest(root.resolve()),
    }
    assert len(full["tree_baseline"]) >= 2
    return {
        "authority": authority,
        "root": root,
        "full": full,
        "card": projection.project_workspace_metadata(full),
        "metadata_path": process_dir / f"{REQUEST_ID}.request.json",
    }


def _verify(env: dict[str, Any], workspace: dict[str, Any]) -> str:
    return core._verify_retained_zero_diff(
        request_id=REQUEST_ID,
        task_id=TASK_ID,
        authority_repo=env["authority"],
        retained=env["root"],
        workspace=workspace,
        card_claim_epoch=1,
        claim_epoch=1,
    )


def _write_metadata(env: dict[str, Any], workspace: dict[str, Any]) -> None:
    env["metadata_path"].write_text(
        json.dumps({"request_id": REQUEST_ID, "task_id": TASK_ID, "workspace": workspace}),
        encoding="utf-8",
    )


def test_zero_diff_proves_unchanged_worktree_from_stand_in(retained: dict[str, Any]) -> None:
    _write_metadata(retained, retained["full"])
    assert projection.is_stand_in(retained["card"]["tree_baseline"])
    assert _verify(retained, retained["card"]) == core._ZERO_DIFF_PROVEN
    # A real change on disk is still never proven.
    (retained["root"] / "src" / "new_file.py").write_text("x = 1\n", encoding="utf-8")
    assert _verify(retained, retained["card"]) != core._ZERO_DIFF_PROVEN


def test_zero_diff_fails_closed_without_matching_on_disk_metadata(
    retained: dict[str, Any],
) -> None:
    # Missing on-disk metadata.
    assert _verify(retained, retained["card"]) == core._ZERO_DIFF_UNSEALED_TREE
    # On-disk map whose digest differs from the card stand-in.
    tampered = copy.deepcopy(retained["full"])
    first = sorted(tampered["tree_baseline"])[0]
    tampered["tree_baseline"][first] = "file:644:" + "f" * 64
    _write_metadata(retained, tampered)
    assert _verify(retained, retained["card"]) == core._ZERO_DIFF_UNSEALED_TREE
    # Same map, but the metadata belongs to another worktree path.
    elsewhere = copy.deepcopy(retained["full"])
    elsewhere["path"] = str(retained["root"].parent / "other")
    _write_metadata(retained, elsewhere)
    assert _verify(retained, retained["card"]) == core._ZERO_DIFF_UNSEALED_TREE


def test_zero_diff_legacy_inline_map_needs_no_metadata(retained: dict[str, Any]) -> None:
    assert not retained["metadata_path"].exists()
    assert _verify(retained, retained["full"]) == core._ZERO_DIFF_PROVEN
