"""NF-2026-02241: four manager-tool gaps.

1. ``aiworkhub_task_amend_pending`` amends an UNLAUNCHED pending card (add-only
   allowed_writes/read_first, replacement validation) and refuses every other
   card with a typed error.
2. ``manager_correction.allowed_writes_amendment`` lets an accept-time patch
   touch exact amended paths; without it the old refusal stands.
3. The card ``evidence`` field is validated, persisted and carried into the
   worker's task contract.
4. task_show / agent_task_status brief report ``reject_count``, ``reject_cap``
   and the per-lens required-reviewer state.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, launch_replay_guard, manager_card_amend  # noqa: E402
from aiworkhub import process_launcher_accept_review as accept_review  # noqa: E402
from aiworkhub import quality_evidence, task_store  # noqa: E402

TEST_FILE = "tests/test_widget.py"
VALIDATION = f"pytest -q {TEST_FILE}"


@pytest.fixture
def manager_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / TEST_FILE).write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "notes.md").write_text("notes\n", encoding="utf-8")
    task_store.initialize_repository(root)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {"provider": "claude", "session_id": "SESSION-A", "route_state": "verified"},
    )
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **k: None)
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda _runner: (True, "test_route"))
    return root


def _insert_card(repo: Path, task_id: str, **overrides: Any) -> None:
    _readiness, db_path = task_store._require_ready(repo)
    card: dict[str, Any] = {
        "task_id": task_id,
        "runner": "claude-worker",
        "topic": "coding",
        "status": "pending",
        "worker_status": "unclaimed",
        "objective": "do the thing",
        "allowed_writes": ["src/widget.py"],
        "read_first": [],
        "forbidden": [],
        "validation": [VALIDATION],
        "validation_roles": ["generic"],
        "work_kind": "generic",
        "history": [{"event": "created"}],
    }
    card.update(overrides)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, archived_at, started_at, claimed_by) "
            "VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?)",
            (
                task_id, card["runner"], card["topic"], card["status"], card["worker_status"],
                json.dumps(card), "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
                str(overrides.get("archived_at") or ""), overrides.get("started_at"),
                overrides.get("claimed_by"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _stored(repo: Path, task_id: str) -> dict[str, Any]:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return json.loads(row[0])


def _events(repo: Path, task_id: str) -> list[tuple[str, dict[str, Any]]]:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT event, payload_json FROM task_events WHERE task_id=? ORDER BY event_id",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()
    return [(row[0], json.loads(row[1])) for row in rows]


# ------------------------------------------------------------ 1. amend pending


def test_amend_pending_widens_scope_and_replaces_validation(manager_repo: Path) -> None:
    _insert_card(manager_repo, "AMEND_1")
    before = _stored(manager_repo, "AMEND_1")

    result = manager_card_amend.amend_pending(
        "AMEND_1",
        allowed_writes=["src/widget.py", "tests/test_widget.py"],
        read_first=["docs/notes.md"],
        validation=[VALIDATION, f"pytest -q {TEST_FILE} -k ok"],
    )

    assert result["ok"] is True, result
    assert result["state"] == "amended"
    assert result["fields"] == ["allowed_writes", "read_first", "validation"]
    assert result["allowed_writes_added"] == ["tests/test_widget.py"]
    assert result["read_first_added"] == ["docs/notes.md"]
    card = _stored(manager_repo, "AMEND_1")
    # Add-only: the existing entry stays first; replacement: the whole list.
    assert card["allowed_writes"] == ["src/widget.py", "tests/test_widget.py"]
    assert card["read_first"] == ["docs/notes.md"]
    assert card["validation"] == [VALIDATION, f"pytest -q {TEST_FILE} -k ok"]
    assert card["validation_roles"] == ["generic", "generic"]
    assert card["history"][-1]["event"] == task_store.CARD_AMENDED_PENDING_EVENT
    assert card["history"][-1]["fields"] == ["allowed_writes", "read_first", "validation"]
    assert result["card_content_sha256"] == task_store.card_content_identity(card)
    assert result["card_content_sha256"] != task_store.card_content_identity(before)
    # The amendment launches nothing: the card is still pristine and amendable.
    assert task_store.is_pristine_pending_card(card)
    assert [event for event, _ in _events(manager_repo, "AMEND_1")] == [
        task_store.CARD_AMENDED_PENDING_EVENT
    ]


def test_server_tool_returns_the_new_card_sha256(manager_repo: Path) -> None:
    from aiworkhub import server

    _insert_card(manager_repo, "AMEND_SRV")
    result = server.aiworkhub_task_amend_pending(
        "AMEND_SRV", allowed_writes=["tests/test_widget.py"]
    )

    assert result["ok"] is True, result
    stored = task_store.get_task(manager_repo, "AMEND_SRV")
    assert result["card_sha256"] == server._card_identity(stored)["card_sha256"]


@pytest.mark.parametrize(
    ("overrides", "refusal"),
    [
        ({"launch_request_id": "req-1"}, "card_already_launched"),
        ({"history": [{"event": "created"}, {"event": "claimed"}]}, "card_already_launched"),
        ({"started_at": "2026-01-02T00:00:00+00:00"}, "card_already_launched"),
        ({"status": "processing", "worker_status": "claimed", "claimed_by": "w"}, "card_claimed"),
        ({"status": "review", "worker_status": "review"}, "card_not_pending:review"),
        ({"status": "blocked", "worker_status": "blocked"}, "card_not_pending:blocked"),
        ({"archived_at": "2026-01-03T00:00:00+00:00"}, "card_archived"),
    ],
)
def test_amend_pending_refuses_every_card_that_is_not_unlaunched_pending(
    manager_repo: Path, overrides: dict[str, Any], refusal: str
) -> None:
    _insert_card(manager_repo, "AMEND_NO", **overrides)
    before = _stored(manager_repo, "AMEND_NO")

    result = manager_card_amend.amend_pending("AMEND_NO", allowed_writes=["tests/test_widget.py"])

    assert result["ok"] is False
    assert result["error"] == f"task_amend_pending_refused:{refusal}"
    assert _stored(manager_repo, "AMEND_NO") == before


def test_store_rechecks_eligibility_inside_its_transaction(manager_repo: Path) -> None:
    _insert_card(manager_repo, "AMEND_RACE", launch_request_id="req-9")

    ok, reason, detail = task_store.amend_pending_card(
        manager_repo, "AMEND_RACE", actor="claude", allowed_writes=["tests/test_widget.py"]
    )

    assert (ok, reason, detail) == (False, "card_already_launched", {})


def test_amend_pending_refusals_are_typed(manager_repo: Path) -> None:
    _insert_card(manager_repo, "AMEND_T")

    assert manager_card_amend.amend_pending("AMEND_T")["error"] == "task_amend_pending_empty"
    missing = manager_card_amend.amend_pending("NOPE", allowed_writes=["a.py"])
    assert missing["error"] == "task_amend_pending_refused:task_not_found"
    reserved = manager_card_amend.amend_pending("AMEND_T", allowed_writes=[".git/config"])
    assert reserved["ok"] is False and reserved["stderr"] == "allowed_write_reserved_path"
    absent = manager_card_amend.amend_pending("AMEND_T", read_first=["docs/absent.md"])
    assert absent["stderr"] == "read_first_amendment_invalid"
    mismatched = manager_card_amend.amend_pending(
        "AMEND_T", validation=[VALIDATION], validation_roles=["generic", "generic"]
    )
    assert mismatched["ok"] is False
    roles_only = manager_card_amend.amend_pending("AMEND_T", validation_roles=["generic"])
    assert roles_only["error"] == "validation_roles_require_validation"
    assert _stored(manager_repo, "AMEND_T")["history"] == [{"event": "created"}]


def test_validation_replacement_keeps_the_work_kind_role_gate(manager_repo: Path) -> None:
    _insert_card(
        manager_repo,
        "AMEND_BUG",
        work_kind="bugfix",
        validation=[VALIDATION, f"pytest -q {TEST_FILE} -k ok"],
        validation_roles=["reproduction", "regression"],
    )

    refused = manager_card_amend.amend_pending("AMEND_BUG", validation=[VALIDATION])

    assert refused["ok"] is False
    assert _stored(manager_repo, "AMEND_BUG")["validation_roles"] == [
        "reproduction", "regression"
    ]


def test_manager_identity_is_required(manager_repo: Path, monkeypatch) -> None:
    _insert_card(manager_repo, "AMEND_ID")
    monkeypatch.setattr(core, "_live_manager_identity", lambda: None)

    result = manager_card_amend.amend_pending("AMEND_ID", allowed_writes=["a.py"])

    assert result["error"] == "manager_identity_required:task_amend_pending"


# ------------------------------------- 2. accept-time allowed_writes amendment

TARGET = "src/widget.py"
NEIGHBOUR = "tests/test_neighbour.py"
REVIEWER_REQUEST_ID = "reviewer-request-1"
LENS = quality_evidence.LENS_CORRECTNESS
FINDING_LOCAL_ID = "correctness-0-abc"
FINDING_ID = f"reviewer:{LENS}:{FINDING_LOCAL_ID}"
PATCH = (
    f"--- a/{TARGET}\n+++ b/{TARGET}\n@@ -1,2 +1,2 @@\n alpha\n-beta\n+gamma\n"
    f"--- a/{NEIGHBOUR}\n+++ b/{NEIGHBOUR}\n@@ -1 +1 @@\n-assert old\n+assert new\n"
)


class _Core:
    repo: Path

    def manager_bootstrap(self) -> dict:
        return {
            "role": "manager",
            "repo": str(self.repo),
            "manager_route": {"provider": "codex", "thread_id": "thread-1"},
        }


def _validate_correction(
    repo: Path, monkeypatch: pytest.MonkeyPatch, **extra: Any
) -> tuple[Any, dict, str]:
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    workspace = repo / "workspace"
    (workspace / "src").mkdir(parents=True, exist_ok=True)
    (workspace / TARGET).write_bytes(b"alpha\nbeta\n")
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / NEIGHBOUR).write_bytes(b"assert old\n")
    stub_core = _Core()
    stub_core.repo = repo
    reviewers = [{
        "request_id": REVIEWER_REQUEST_ID,
        "lens": LENS,
        "usable": True,
        "receipt": {"report": {"lens": LENS, "findings": [
            {"id": FINDING_LOCAL_ID, "disposition": "defect", "severity": "low"}
        ]}},
    }]
    return accept_review.validate_manager_correction(
        SimpleNamespace(repo=repo),
        {"patch": PATCH, "finding_ids": [FINDING_ID], "reason": "fix", **extra},
        card={"allowed_writes": [TARGET], "forbidden": []},
        workspace=SimpleNamespace(path=workspace),
        changed=[TARGET],
        reviewers=reviewers,
        reviewer_request_ids=[REVIEWER_REQUEST_ID],
        finding_dispositions=[{
            "reviewer_request_id": REVIEWER_REQUEST_ID,
            "finding_id": FINDING_ID,
            "disposition": "confirmed",
        }],
        core=stub_core,
        path_cls=Path,
    )


def test_without_the_key_a_path_outside_allowed_writes_is_still_refused(
    tmp_path: Path, monkeypatch
) -> None:
    record, texts, error = _validate_correction(tmp_path.resolve(), monkeypatch)

    assert (record, texts) == (None, {})
    assert error == f"path_outside_allowed_writes:{NEIGHBOUR}"


def test_amended_path_is_accepted_and_patched_against_canonical(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path.resolve()
    record, texts, error = _validate_correction(
        repo, monkeypatch, allowed_writes_amendment=[NEIGHBOUR]
    )

    assert error == ""
    assert record["allowed_writes_amendment"] == [NEIGHBOUR]
    assert record["paths_outside_candidate"] == [NEIGHBOUR]
    assert record["paths"] == sorted([TARGET, NEIGHBOUR])
    assert texts[NEIGHBOUR] == "assert new\n"
    assert texts[TARGET] == "alpha\ngamma\n"


@pytest.mark.parametrize(
    ("amendment", "error"),
    [
        (["docs/untouched.md"], "allowed_writes_amendment_path_untouched:docs/untouched.md"),
        (["tests/*.py"], "allowed_writes_amendment_not_exact_path:tests/*.py"),
        ([], "allowed_writes_amendment_invalid"),
        ("tests/test_neighbour.py", "allowed_writes_amendment_invalid"),
        ([".git/config"], "allowed_writes_amendment_refused:allowed_write_reserved_path"),
    ],
)
def test_amendment_refusals_are_named(tmp_path: Path, monkeypatch, amendment, error) -> None:
    amended = [NEIGHBOUR, *amendment] if error.endswith("untouched.md") else amendment
    record, texts, got = _validate_correction(
        tmp_path.resolve(), monkeypatch, allowed_writes_amendment=amended
    )

    assert (record, texts) == (None, {})
    assert got == error


def _outside_record(*digests: tuple[str, bytes]) -> dict[str, Any]:
    return {
        "paths_outside_candidate": [path for path, _raw in digests],
        manager_card_amend.CANONICAL_PREIMAGE_KEY: {
            path: hashlib.sha256(raw).hexdigest() for path, raw in digests
        },
    }


def test_widen_workspace_seeds_missing_path_and_baseline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / NEIGHBOUR).write_bytes(b"assert old\n")
    workspace = SimpleNamespace(
        path=tmp_path / "ws", allowed_writes=(TARGET,), parent_baseline={TARGET: "x"}
    )

    widened = manager_card_amend.widen_workspace_for_correction(
        workspace, _outside_record((NEIGHBOUR, b"assert old\n")), repo
    )

    assert widened.allowed_writes == (TARGET, NEIGHBOUR)
    assert widened.parent_baseline[TARGET] == "x"
    assert widened.parent_baseline[NEIGHBOUR].startswith("file:")
    assert (tmp_path / "ws" / NEIGHBOUR).read_bytes() == b"assert old\n"
    assert workspace.allowed_writes == (TARGET,)
    assert manager_card_amend.widen_workspace_for_correction(workspace, {}, repo) is workspace


def test_validated_record_binds_the_canonical_preimage_of_outside_paths(
    tmp_path: Path, monkeypatch
) -> None:
    record, _texts, error = _validate_correction(
        tmp_path.resolve(), monkeypatch, allowed_writes_amendment=[NEIGHBOUR]
    )

    assert error == ""
    assert record[manager_card_amend.CANONICAL_PREIMAGE_KEY] == {
        NEIGHBOUR: hashlib.sha256(b"assert old\n").hexdigest()
    }


def test_widen_refuses_when_the_canonical_amended_path_changed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / NEIGHBOUR).write_bytes(b"assert old\n")
    record = _outside_record((NEIGHBOUR, b"assert old\n"))
    # A concurrent accept lands different canonical bytes after validation.
    (repo / NEIGHBOUR).write_bytes(b"assert concurrent\n")
    workspace = SimpleNamespace(path=tmp_path / "ws", allowed_writes=(TARGET,), parent_baseline={})

    with pytest.raises(manager_card_amend.AmendedPathCanonicalChanged) as raised:
        manager_card_amend.widen_workspace_for_correction(workspace, record, repo)

    assert str(raised.value) == f"amended_path_canonical_changed:{NEIGHBOUR}"
    assert not (tmp_path / "ws" / NEIGHBOUR).exists()
    assert (repo / NEIGHBOUR).read_bytes() == b"assert concurrent\n"
    missing = {"paths_outside_candidate": [NEIGHBOUR]}
    with pytest.raises(manager_card_amend.AmendedPathCanonicalChanged, match="hash_missing"):
        manager_card_amend.widen_workspace_for_correction(workspace, missing, repo)


@pytest.mark.parametrize(
    "bad", ["../x.py", "tests/../../x.py", "/abs/x.py", "C:/x.py", "//server/share/x.py"]
)
def test_amended_path_outside_the_workspace_is_refused_before_seeding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace = SimpleNamespace(path=ws, allowed_writes=(TARGET,), parent_baseline={})
    created: list[Path] = []
    expected = f"amended_path_outside_workspace:{bad}"

    with pytest.raises(manager_card_amend.AmendedPathOutsideWorkspace) as raised:
        manager_card_amend.widen_workspace_for_correction(
            workspace, _outside_record((bad, b"x\n")), repo, seeded=created
        )

    assert str(raised.value) == expected
    assert created == []
    assert list(ws.iterdir()) == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["repo", "ws"]
    # Refused where the amendment is first accepted, so it never reaches a receipt.
    card = {"allowed_writes": [TARGET], "forbidden": []}
    assert manager_card_amend.correction_allowed_writes_amendment(
        [bad], card=card, root=repo
    ) == ([], expected)
    assert manager_card_amend.correction_allowed_writes_amendment(
        [bad], card=card
    ) == ([], expected)
    record, texts, error = _validate_correction(
        tmp_path.resolve() / "validate", monkeypatch, allowed_writes_amendment=[bad]
    )
    assert (record, texts, error) == (None, {}, expected)


def test_amended_path_under_a_symlinked_parent_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / NEIGHBOUR).write_bytes(b"assert old\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, ws / "tests", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("os.symlink is not permitted here")
    workspace = SimpleNamespace(path=ws, allowed_writes=(TARGET,), parent_baseline={})
    expected = f"amended_path_outside_workspace:{NEIGHBOUR}"

    assert manager_card_amend.amended_path_outside_workspace(NEIGHBOUR, ws) == expected
    with pytest.raises(manager_card_amend.AmendedPathOutsideWorkspace) as raised:
        manager_card_amend.widen_workspace_for_correction(
            workspace, _outside_record((NEIGHBOUR, b"assert old\n")), repo
        )

    assert str(raised.value) == expected
    assert list(outside.iterdir()) == []


def test_restore_removes_seeded_files_and_directories_newest_first(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = "tests/seeded_nf02241/deeper/test_x.py"
    (repo / nested).parent.mkdir(parents=True)
    (repo / nested).write_bytes(b"assert old\n")
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    workspace = SimpleNamespace(path=ws, allowed_writes=(TARGET,), parent_baseline={})
    originals = manager_card_amend.CorrectionOriginals()
    assert not originals

    widened = manager_card_amend.widen_workspace_for_correction(
        workspace, _outside_record((nested, b"assert old\n")), repo, seeded=originals.created
    )

    assert originals.created == [
        ws / "tests" / "seeded_nf02241",
        ws / "tests" / "seeded_nf02241" / "deeper",
        ws / nested,
    ]
    assert originals  # truthy on seeding alone
    originals[nested] = (ws / nested).read_bytes()
    (ws / nested).write_bytes(b"assert new\n")

    assert accept_review._restore_correction_originals(widened, originals) == []
    assert not (ws / "tests" / "seeded_nf02241").exists()
    assert (ws / "tests").is_dir()
    assert not originals
    # A second cleanup is a no-op and recreates nothing.
    assert accept_review._restore_correction_originals(widened, originals) == []
    assert not (ws / nested).exists()
    assert (repo / nested).read_bytes() == b"assert old\n"


def test_accept_review_promotes_an_amended_path_outside_allowed_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The existing accept_review end-to-end fixture (real accept_review, real
    # task_store, real task_engine guard), imported as its siblings do.
    tests_dir = str(Path(__file__).resolve().parent)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import test_accept_with_manager_correction_nf01436 as nf01436

    manager = nf01436._accept_env(tmp_path, monkeypatch)
    neighbour = "tests/test_demo_neighbour.py"
    (manager.repo / "tests").mkdir(parents=True, exist_ok=True)
    (manager.repo / neighbour).write_bytes(b"assert old\n")
    monkeypatch.setattr(core, "repo_root", lambda: manager.repo)
    assert neighbour not in task_store.get_task(manager.repo, nf01436.TASK_ID)["allowed_writes"]
    patch = nf01436._patch() + (
        f"--- a/{neighbour}\n+++ b/{neighbour}\n@@ -1 +1 @@\n-assert old\n+assert new\n"
    )

    result = nf01436._accept(
        manager,
        manager_correction={
            "patch": patch,
            "finding_ids": [nf01436.FINDING_ID],
            "reason": "the accept revalidation showed the neighbour test red",
            "allowed_writes_amendment": [neighbour],
        },
    )

    assert result["ok"] is True, result.get("error")
    assert sorted(result["promoted_paths"]) == sorted([nf01436.TARGET, neighbour])
    # The amended path landed in the canonical tree with the corrected bytes.
    assert (manager.repo / neighbour).read_bytes() == b"assert new\n"
    assert (manager.repo / nf01436.TARGET).read_text(encoding="utf-8") == nf01436.CORRECTED
    receipt = result["accepted_outcome_receipt"]
    assert receipt["manager_correction"]["paths"] == sorted([nf01436.TARGET, neighbour])
    assert neighbour in task_store.get_task(manager.repo, nf01436.TASK_ID)["allowed_writes"]


def test_failed_accept_removes_the_seeded_amended_path_and_its_new_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tests_dir = str(Path(__file__).resolve().parent)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import test_accept_with_manager_correction_nf01436 as nf01436

    manager = nf01436._accept_env(tmp_path, monkeypatch)
    neighbour = "tests/amended_nf02241/deeper/test_seeded.py"
    (manager.repo / neighbour).parent.mkdir(parents=True, exist_ok=True)
    (manager.repo / neighbour).write_bytes(b"assert old\n")
    monkeypatch.setattr(core, "repo_root", lambda: manager.repo)
    real_widen = manager_card_amend.widen_workspace_for_correction
    seen: dict[str, Any] = {}

    def _widen(workspace: Any, record: Any, repo: Path, *, seeded: Any = None) -> Any:
        widened = real_widen(workspace, record, repo, seeded=seeded)
        seen["root"] = Path(widened.path)
        seen["created"] = list(seeded or ())
        return widened

    def _fail_after_seeding(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("forced_after_seeding")

    monkeypatch.setattr(manager_card_amend, "widen_workspace_for_correction", _widen)
    monkeypatch.setattr(accept_review, "_finalizer_validation_reuse", _fail_after_seeding)
    patch = nf01436._patch() + (
        f"--- a/{neighbour}\n+++ b/{neighbour}\n@@ -1 +1 @@\n-assert old\n+assert new\n"
    )

    try:
        result = nf01436._accept(
            manager,
            manager_correction={
                "patch": patch,
                "finding_ids": [nf01436.FINDING_ID],
                "reason": "the accept revalidation showed the neighbour test red",
                "allowed_writes_amendment": [neighbour],
            },
        )
    except RuntimeError as exc:
        assert "forced_after_seeding" in str(exc)
    else:
        assert result["ok"] is False, result

    root = seen["root"]
    created = seen["created"]
    # Seeding created the file and the directories it needed, then the accept failed.
    assert root / neighbour in created
    assert root / "tests" / "amended_nf02241" in created
    assert root / "tests" / "amended_nf02241" / "deeper" in created
    assert [path for path in created if path.exists()] == []
    assert not (root / "tests" / "amended_nf02241").exists()
    # The canonical file is untouched and the card was not widened.
    assert (manager.repo / neighbour).read_bytes() == b"assert old\n"
    assert neighbour not in task_store.get_task(manager.repo, nf01436.TASK_ID)["allowed_writes"]


def test_landed_amendment_is_recorded_on_the_card_add_only(manager_repo: Path) -> None:
    _insert_card(manager_repo, "ACCEPTED", status="finished", worker_status="done")

    result = manager_card_amend.record_accept_amendment(
        manager_repo,
        task_id="ACCEPTED",
        request_id="req-7",
        record={"allowed_writes_amendment": [NEIGHBOUR], "actor": "codex:thread-1"},
    )

    assert result["state"] == "amended" and result["applied"] == [NEIGHBOUR]
    card = _stored(manager_repo, "ACCEPTED")
    assert card["allowed_writes"] == ["src/widget.py", NEIGHBOUR]
    assert card["allowed_writes_amendments"][-1]["request_id"] == "req-7"
    assert _events(manager_repo, "ACCEPTED")[-1][0] == (
        task_store.ALLOWED_WRITES_AMENDED_ON_ACCEPT_EVENT
    )
    assert manager_card_amend.record_accept_amendment(
        manager_repo, task_id="ACCEPTED", request_id="req-7", record={"paths": [TARGET]}
    ) is None


# ----------------------------------------------------------------- 3. evidence


def _create_args(task_id: str, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = dict(
        task_id=task_id,
        title="evidence field",
        runner="claude-worker",
        topic="task_create",
        objective="exercise the card evidence field",
        acceptance=["evidence persists"],
        allowed_writes=["src/aiworkhub/example_target.py"],
        required_outputs=["src/aiworkhub/example_target.py"],
        validation=[VALIDATION],
        custom_template_escape="audited_custom_unclassified",
        callback_required=False,
    )
    args.update(overrides)
    return args


def test_create_persists_evidence_and_the_contract_carries_it(manager_repo: Path) -> None:
    created = core.create_task(**_create_args("EVID_1", evidence="  measured: 3/3 red\n"))

    assert created["ok"] is True, created
    card = _stored(manager_repo, "EVID_1")
    assert card["evidence"] == "measured: 3/3 red"
    keys = task_store.TASK_CONTRACT_KEYS
    assert keys.index("evidence") == keys.index("objective") + 1
    assert "evidence" in task_store.CARD_CONTENT_IDENTITY_KEYS
    without = {key: value for key, value in card.items() if key != "evidence"}
    assert task_store.card_content_identity(card) != task_store.card_content_identity(without)


def test_create_without_evidence_stores_no_key(manager_repo: Path) -> None:
    created = core.create_task(**_create_args("EVID_0"))

    assert created["ok"] is True, created
    assert "evidence" not in _stored(manager_repo, "EVID_0")


def test_over_cap_evidence_is_refused_and_nothing_is_stored(manager_repo: Path) -> None:
    cap = manager_card_amend.EVIDENCE_MAX_CHARS
    created = core.create_task(**_create_args("EVID_BIG", evidence="x" * (cap + 1)))

    assert created["ok"] is False
    assert created["stderr"] == (
        f"invalid_evidence:too_long:{cap + 1}_chars_exceeds_limit_{cap}"
    )
    assert created["violations"][0]["field"] == "evidence"
    assert task_store.get_task(manager_repo, "EVID_BIG") is None


def test_evidence_rules() -> None:
    cap = manager_card_amend.EVIDENCE_MAX_CHARS
    assert manager_card_amend.evidence_violations(None) == []
    assert manager_card_amend.evidence_violations("   ") == []
    assert manager_card_amend.evidence_violations("x" * cap) == []
    assert manager_card_amend.evidence_violations("a\tb\nc\r\n") == []
    assert manager_card_amend.evidence_violations("a\x00b")[0]["code"] == (
        "invalid_evidence:control_character:U+0000"
    )
    assert manager_card_amend.evidence_violations(7)[0]["code"] == (
        "invalid_evidence:not_a_string:int"
    )
    assert manager_card_amend.evidence_card_fields("  ") == {}
    assert manager_card_amend.evidence_card_fields(" e ") == {"evidence": "e"}


def test_needfix_plan_evidence_is_split_bound_and_resolved_by_digest() -> None:
    plan = {"title": "t", "evidence": "measured"}
    rest, evidence = manager_card_amend.split_plan_evidence(plan)
    assert rest == {"title": "t"} and evidence == "measured" and "evidence" in plan
    assert manager_card_amend.split_plan_evidence({"title": "t"}) == ({"title": "t"}, None)

    preview = manager_card_amend.bind_preview_evidence({"plan_digest": "d-1"}, evidence)
    assert preview["evidence_chars"] == len("measured")
    assert manager_card_amend.resolve_convert_evidence(None, "d-1") == "measured"
    assert manager_card_amend.resolve_convert_evidence("explicit", "d-1") == "explicit"
    assert manager_card_amend.resolve_convert_evidence(None, "unknown") is None

    refused = manager_card_amend.bind_preview_evidence(
        {"plan_digest": "d-2"}, "x" * (manager_card_amend.EVIDENCE_MAX_CHARS + 1)
    )
    assert refused["refusal_reason"].startswith("invalid_evidence:too_long:")
    # A digest whose preview refused its evidence never commits without it.
    with pytest.raises(manager_card_amend.PreviewEvidenceUnavailable) as raised:
        manager_card_amend.resolve_convert_evidence(None, "d-2")
    assert str(raised.value) == manager_card_amend.PREVIEW_EVIDENCE_INVALID


def _digest_only_convert(monkeypatch: pytest.MonkeyPatch, digest: str) -> tuple[Any, list]:
    """Drive server.needfix_convert digest-only; return its reply and core's kwargs."""
    from aiworkhub import server

    calls: list[dict[str, Any]] = []

    def _convert(needfix_id, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "returncode": 0, "task_id": "needfix-NF-TEST"}

    monkeypatch.setattr(server.core, "needfix_convert", _convert)
    monkeypatch.setattr(
        server, "_normalize_needfix_task_plan", lambda _nid, plan: (plan, [], {})
    )
    monkeypatch.setattr(server, "_needfix_plan_receipt", lambda reply, **_k: reply)
    return server.needfix_convert("NF-TEST", plan_digest=digest), calls


def test_re_preview_without_evidence_replaces_the_earlier_evidence(monkeypatch) -> None:
    digest = "d-re-preview"
    manager_card_amend.bind_preview_evidence({"plan_digest": digest}, "measured: 3/3 red")
    assert manager_card_amend.resolve_convert_evidence(None, digest) == "measured: 3/3 red"

    # The same digest is previewed again, this time declaring no evidence.
    again = manager_card_amend.bind_preview_evidence({"plan_digest": digest}, None)
    assert "evidence_chars" not in again

    reply, calls = _digest_only_convert(monkeypatch, digest)
    assert reply["ok"] is True
    assert calls == [{"task_plan": None, "plan_digest": digest}]
    assert "evidence" not in calls[0]

    # Blank evidence and a refused value replace it too.
    manager_card_amend.bind_preview_evidence({"plan_digest": digest}, "measured")
    manager_card_amend.bind_preview_evidence({"plan_digest": digest}, "   ")
    assert manager_card_amend.resolve_convert_evidence(None, digest) is None
    manager_card_amend.bind_preview_evidence({"plan_digest": digest}, "measured")
    manager_card_amend.bind_preview_evidence({"plan_digest": digest}, "a\x00b")
    with pytest.raises(manager_card_amend.PreviewEvidenceUnavailable):
        manager_card_amend.resolve_convert_evidence(None, digest)


def test_evicted_preview_evidence_refuses_a_digest_only_convert(monkeypatch) -> None:
    digest = "d-evicted"
    manager_card_amend.bind_preview_evidence({"plan_digest": digest}, "measured")
    # Force eviction: the text cache keeps only the most recent previews.
    for index in range(manager_card_amend._PREVIEW_EVIDENCE_LIMIT):
        manager_card_amend.bind_preview_evidence({"plan_digest": f"d-filler-{index}"}, "x")
    assert digest not in manager_card_amend._PREVIEW_EVIDENCE

    with pytest.raises(manager_card_amend.PreviewEvidenceUnavailable) as raised:
        manager_card_amend.resolve_convert_evidence(None, digest)
    assert str(raised.value) == manager_card_amend.PREVIEW_EVIDENCE_EXPIRED

    reply, calls = _digest_only_convert(monkeypatch, digest)
    assert reply["ok"] is False
    assert reply["error"] == (
        "preview_evidence_expired:re-run needfix_preview_convert"
    )
    assert calls == []
    # Explicit evidence on the convert still wins over the expired marker.
    assert manager_card_amend.resolve_convert_evidence("explicit", digest) == "explicit"


# ---------------------------------------------------- 4. reject depth + lenses


def _reviewed_card(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": "review",
        "worker_status": "review",
        "allowed_writes": ["src/widget.py"],
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "changed_paths": ["src/widget.py"],
                "request_identity": {"request_id": "req-target"},
            },
        },
    }


def _insert_reject(repo: Path, task_id: str, payload: dict[str, Any]) -> None:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, ?, 'codex', ?, '2026-01-01T00:00:00+00:00')",
            (task_id, launch_replay_guard._REJECT_EVENT, json.dumps(payload)),
        )
        conn.commit()
    finally:
        conn.close()


def test_review_state_counts_rejects_and_reports_each_required_lens(
    manager_repo: Path, monkeypatch
) -> None:
    card = _reviewed_card("REVIEWED")
    identity = launch_replay_guard.task_contract_identity(card)
    counted = {
        "to": "pending",
        "failure_category": launch_replay_guard.REJECT_DEPTH_COUNTED_CATEGORY,
        "failure_category_source": "manager",
        "contract_identity": identity,
        "request_id": "req-a",
    }
    _insert_reject(manager_repo, "REVIEWED", counted)
    _insert_reject(manager_repo, "REVIEWED", {**counted, "actor": launch_replay_guard.AUTO_REWORK_ACTOR})
    monkeypatch.setattr(
        quality_evidence,
        "resolve_risk_profile",
        lambda *_a, **_k: {"required_reviewer_lenses": ["correctness", "security", "performance"]},
    )
    seen: list[tuple[str, str]] = []

    def _reviewers(_manager, task_id, request_id):
        seen.append((task_id, request_id))
        return [
            {"lens": "correctness", "usable": True, "request_id": "rv-1",
             "receipt": {"report": {}}, "running": False},
            {"lens": "security", "usable": False, "request_id": "rv-2",
             "running": True, "running_request_id": "rv-3"},
        ]

    monkeypatch.setattr(accept_review, "reviewer_evidence", _reviewers)
    factory_calls: list[int] = []

    def _factory():
        factory_calls.append(1)
        return SimpleNamespace(repo=manager_repo)

    fields = manager_card_amend.review_state_fields(card, repo=manager_repo, manager=_factory)

    assert fields["reject_cap"] == launch_replay_guard.REJECT_DEPTH_CAP
    assert fields["reject_count"] == 1
    assert seen == [("REVIEWED", "req-target")] and factory_calls == [1]
    assert fields["required_reviewer_lenses"] == [
        {"lens": "correctness", "state": "verified", "reviewer_request_id": "rv-1",
         "has_verified_report": True},
        {"lens": "security", "state": "running", "reviewer_request_id": "rv-3",
         "has_verified_report": False},
        {"lens": "performance", "state": "missing", "reviewer_request_id": "",
         "has_verified_report": False},
    ]


def test_review_state_unknowns_are_reported_not_zero(tmp_path: Path, monkeypatch) -> None:
    card = _reviewed_card("UNKNOWN")
    monkeypatch.setattr(
        quality_evidence,
        "resolve_risk_profile",
        lambda *_a, **_k: {"required_reviewer_lenses": ["correctness"]},
    )
    monkeypatch.setattr(
        accept_review,
        "reviewer_evidence",
        lambda *_a: [{"lens": "", "usable": False, "state_unknown": True}],
    )

    fields = manager_card_amend.review_state_fields(
        card, repo=tmp_path / "no-store", manager=SimpleNamespace(repo=tmp_path)
    )

    assert fields["reject_count"] is None
    assert fields["reject_count_unavailable"]
    assert fields["required_reviewer_lenses"][0]["state"] == "state_unknown"
    assert manager_card_amend.review_state_fields(card, repo=None, manager=None)[
        "reject_count_unavailable"
    ] == "repository_or_task_unresolved"


def test_task_show_reports_reject_count_cap_and_lenses(manager_repo: Path, monkeypatch) -> None:
    from aiworkhub import server

    card = _reviewed_card("SHOWN")
    monkeypatch.setattr(
        core,
        "show_task",
        lambda task_id, full=False: {
            "ok": True, "returncode": 0, "command": [], "stderr": "",
            "stdout": json.dumps(card),
        },
    )
    monkeypatch.setattr(accept_review, "reviewer_evidence", lambda *_a: [])
    monkeypatch.setattr(server.process_launcher, "default_manager", lambda: SimpleNamespace(repo=manager_repo))

    result = server.aiworkhub_task_show("SHOWN")

    assert result["reject_cap"] == launch_replay_guard.REJECT_DEPTH_CAP
    assert result["reject_count"] == 0
    lenses = result["required_reviewer_lenses"]
    assert isinstance(lenses, list) and lenses
    assert all(entry["state"] == "missing" for entry in lenses)
    assert {"lens", "state", "reviewer_request_id", "has_verified_report"} == set(lenses[0])
