"""NF-2026-00708: readonly acceptance must bind a real accepted-outcome receipt.

Before this fix, ``process_launcher_accept_review.accept_review``'s readonly
research branch called ``task_engine.accept_review`` without an
``accepted_outcome_receipt`` kwarg. ``task_engine.accept_review`` unconditionally
requires one (``_validate_accepted_outcome_receipt`` fails closed on ``None``),
so every readonly research acceptance failed with
``research_finalize_failed:accepted_outcome_receipt_missing`` even though the
sealed terminal evidence was otherwise valid. The identical omission also
existed in the readonly quality-review branch, failing with
``quality_review_finalize_failed:accepted_outcome_receipt_missing``. These
tests drive the real ``ProcessManager.accept_review`` entry point against a
real ``task_store`` repository and the real, unmodified
``task_engine.accept_review`` guard -- nothing about receipt validation is
mocked -- so they fail on the pre-fix code for exactly that reason and pass
once each readonly branch builds and binds the canonical zero-promotion
receipt via ``_accepted_outcome_receipt``.

Collaborators unrelated to the guard under test (workspace shape, git scope
enforcement, declared validations, quality-review receipt verification) are
replaced via the documented ``process_launcher_accept_review`` seam
mechanism, the same mechanism 27 other test files already rely on.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher, task_engine, task_store  # noqa: E402

REQUEST_ID = "req-nf708-readonly"
TASK_ID = "TASK_NF708"
RUNNER = "codex_worker_nf708"
TOPIC = "task_mcp"
BASE_OID = "base-oid-nf708"
EVIDENCE_REFERENCE = "file:///nf708/evidence"


class _FakeWorkspace:
    def __init__(self, *, repo: Path, request_id: str, path: Path, home: Path) -> None:
        self.repo = repo
        self.request_id = request_id
        self.path = path
        self.home = home

    def as_metadata(self) -> dict:
        return {
            "request_id": self.request_id,
            "repo": str(self.repo),
            "path": str(self.path),
            "home": str(self.home),
        }


class _FakeWorkerWorkspace:
    @staticmethod
    def from_metadata(meta: dict) -> _FakeWorkspace:
        return _FakeWorkspace(
            repo=Path(meta["repo"]),
            request_id=meta["request_id"],
            path=Path(meta["path"]),
            home=Path(meta["home"]),
        )


class _FakeEvidenceRecord:
    def __init__(self, raw: dict | None) -> None:
        self._raw = dict(raw or {})
        self.evidence_level = "fixed_and_verified"
        self.reference = self._raw.get("reference", "")

    def to_dict(self) -> dict:
        return dict(self._raw)


def _fake_evidence_levels() -> SimpleNamespace:
    return SimpleNamespace(
        EvidenceLevel=SimpleNamespace(FIXED_AND_VERIFIED="fixed_and_verified"),
        EvidenceValidationError=ValueError,
        validate_evidence_record=lambda raw: _FakeEvidenceRecord(raw),
        meets_evidence_level=lambda level, minimum: True,
    )


class _StubManager:
    """The manager collaborators ``accept_review`` needs, minus the guard."""

    def __init__(self, *, repo: Path, process_dir: Path, card: dict, latest_event: dict) -> None:
        self.repo = repo
        self.process_dir = process_dir
        self.card = card
        self.latest_event = latest_event
        self.retention_events: list[tuple[dict, str]] = []
        self.needfix_calls: list[tuple[str, str]] = []

    def _request_events(self, request_id: str) -> list[dict]:
        return [self.latest_event]

    @staticmethod
    def _metadata_from_events(events: list[dict]) -> Path | None:
        for event in reversed(events):
            raw = event.get("metadata_path")
            if raw:
                return Path(str(raw))
        return None

    def _show_task(self, task_id: str) -> dict:
        return {"returncode": 0, "stdout": json.dumps(self.card)}

    def _request_lock(self, request_id: str):
        return nullcontext()

    def _promotion_lock(self):
        return nullcontext()

    def _close_accepted_task_needfix(self, task_id: str, request_id: str) -> dict:
        self.needfix_calls.append((task_id, request_id))
        return {"state": "not_attempted"}

    def _context_write_intent_snapshot(self, request_id: str) -> dict:
        return {"ok": False}

    def _verify_attempt_artifact_receipt(self, request_id: str, raw: object) -> dict:
        assert isinstance(raw, dict)
        return dict(raw)

    def _minimum_acceptance_evidence_level(self, card, **_kwargs):
        return "min"

    def _attempt_evidence_reference(self, request_id: str, attempt_artifact_receipt: dict) -> str:
        return EVIDENCE_REFERENCE

    def _canonical_outcome_evidence(self, request_id, attempt_artifact_receipt, *, level, verified_by, message):
        return {"reference": EVIDENCE_REFERENCE, "level": str(level), "verified_by": verified_by, "message": message}

    def _retention_event(self, payload: dict, *, disposition: str) -> None:
        self.retention_events.append((payload, disposition))


def _manifest() -> dict:
    return {
        "schema_id": "aiworkhub.attempt_artifact_bundle_receipt.v1",
        "attempt_id": REQUEST_ID,
        "verified": True,
        "manifest_path": "/does/not/matter",
        "manifest_sha256": "0" * 64,
    }


def _card(*, repo: Path, workspace_path: Path, workspace_home: Path, research_result: dict) -> dict:
    return {
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": TOPIC,
        "status": "review",
        "worker_status": "review",
        "claimed_by": RUNNER,
        "read_only": True,
        "allowed_writes": [],
        "required_outputs": [],
        "claim_epoch": 4,
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {
                    "request_id": REQUEST_ID,
                    "task_id": TASK_ID,
                    "runner": RUNNER,
                    "topic": TOPIC,
                },
                "changed_paths": [],
                "changed_path_hashes": {},
                "workspace": {
                    "request_id": REQUEST_ID,
                    "repo": str(repo),
                    "path": str(workspace_path),
                    "home": str(workspace_home),
                    "base_oid": BASE_OID,
                },
                "attempt_artifact_manifest": _manifest(),
                "evidence_record": {"reference": EVIDENCE_REFERENCE},
                "research_result": research_result,
            },
        },
    }


def _seed_repo(
    tmp_path: Path,
    card: dict,
    *,
    task_id: str = TASK_ID,
    runner: str = RUNNER,
    topic: str = TOPIC,
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-09-08T00:00:00+00:00"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, started_at, "
            "origin_thread_id) VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                runner,
                topic,
                "review",
                "review",
                json.dumps(card),
                now,
                now,
                runner,
                now,
                now,
                "thread-nf708",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return repo


@pytest.fixture
def _readonly_research_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    process_dir = tmp_path / "process_dir"
    process_dir.mkdir()
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    workspace_home = tmp_path / "workspace_home"
    workspace_home.mkdir()

    stdout_path = process_dir / f"{REQUEST_ID}.stdout.log"
    stdout_path.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "NF-2026-00708 readonly research finding: verified.",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # Use the real evidence extractor so the stored/sealed research_result is
    # byte-identical to what the real acceptance path recomputes -- this seam
    # is intentionally left unpatched.
    research_result = process_launcher._readonly_research_result_evidence(stdout_path)
    assert research_result["meaningful_output"] is True

    card = _card(
        repo=tmp_path / "repo",
        workspace_path=workspace_path,
        workspace_home=workspace_home,
        research_result=research_result,
    )
    repo = _seed_repo(tmp_path, card)
    # ``_card`` was built before the repo path existed on disk; the "repo"
    # field inside the sealed workspace metadata must match ``self.repo``
    # exactly, and it already does since both derive from ``tmp_path / "repo"``.
    assert card["terminal_review"]["evidence"]["workspace"]["repo"] == str(repo)

    latest_event = {
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": TOPIC,
        "stdout_path": str(stdout_path),
        "adapter_id": "claude_cli",
    }
    manager = _StubManager(repo=repo, process_dir=process_dir, card=card, latest_event=latest_event)

    monkeypatch.setattr(process_launcher, "WorkerWorkspace", _FakeWorkerWorkspace)
    monkeypatch.setattr(process_launcher, "assert_gc_safe_workspace_shape", lambda *a, **k: None)
    monkeypatch.setattr(process_launcher, "enforce_scope", lambda *a, **k: [])
    monkeypatch.setattr(
        process_launcher,
        "_worker_workspace",
        SimpleNamespace(finalization_git_timeout_seconds=lambda: 5.0),
    )
    monkeypatch.setattr(process_launcher, "cleanup_workspace", lambda *a, **k: None)
    monkeypatch.setattr(process_launcher, "_run_declared_validations", lambda *a, **k: [])
    monkeypatch.setattr(process_launcher, "validate_required_outputs", lambda *a, **k: [])
    monkeypatch.setattr(process_launcher, "evidence_levels", _fake_evidence_levels())
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")

    return manager


def test_readonly_research_accept_review_binds_accepted_outcome_receipt(
    _readonly_research_fixture,
) -> None:
    manager = _readonly_research_fixture

    result = process_launcher.ProcessManager.accept_review(manager, REQUEST_ID, TASK_ID)

    assert result["ok"] is True, result
    assert result["promoted_paths"] == []

    receipt = result["accepted_outcome_receipt"]
    assert receipt["schema_id"] == task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA
    assert receipt["task_id"] == TASK_ID
    assert receipt["request_id"] == REQUEST_ID
    assert receipt["claim_epoch"] == 4
    assert receipt["base_oid"] == BASE_OID
    assert receipt["promoted_paths"] == []
    assert receipt["changed_path_hashes"] == {}
    assert receipt["repository_revision"].startswith("sha256:")
    assert receipt["receipt_id"].startswith("sha256:")

    # The real TaskEngine guard actually ran and finished the task -- this is
    # not a manually markdone task, it is the outcome of real receipt validation.
    finished_card = task_store.get_task(manager.repo, TASK_ID)
    assert finished_card is not None
    assert finished_card["status"] == "finished"
    assert finished_card["accepted_request_id"] == REQUEST_ID
    assert finished_card["accept_evidence"]["accepted_outcome_receipt"] == receipt


def test_readonly_research_accept_review_fails_closed_without_receipt(
    _readonly_research_fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the exact pre-fix regression: dropping the receipt kwarg must fail
    closed with the same error the live bug reproduced, through the real
    unmodified TaskEngine guard -- proving the guard itself was never relaxed."""
    manager = _readonly_research_fixture
    real_accept_review = task_engine.accept_review

    def _accept_review_without_receipt(*args, **kwargs):
        kwargs.pop("accepted_outcome_receipt", None)
        return real_accept_review(*args, **kwargs)

    monkeypatch.setattr(process_launcher, "task_engine", SimpleNamespace(
        accept_review=_accept_review_without_receipt,
        disposition_reviewer_children=task_engine.disposition_reviewer_children,
    ))

    result = process_launcher.ProcessManager.accept_review(manager, REQUEST_ID, TASK_ID)

    assert result["ok"] is False
    assert result["error"] == "research_finalize_failed:accepted_outcome_receipt_missing"


QR_REQUEST_ID = "req-nf708-quality-review"
QR_TASK_ID = "TASK_NF708_QR"


def _quality_review_receipt() -> dict:
    return {
        "schema_id": "aiworkhub.quality_reviewer_receipt.v1",
        "target": {
            "request_id": QR_REQUEST_ID,
            "task_id": QR_TASK_ID,
            "claim_epoch": 4,
        },
        "reviewer": {
            "request_id": "req-nf708-reviewer",
            "task_id": "TASK_NF708_REVIEWER",
            "provider": "claude_cli",
        },
        "report": {
            "lens": "quality",
            "provider": "claude_cli",
            "read_only": True,
            "can_mutate_repo": False,
            "findings": [],
        },
        "authority": {
            "process_identity_verified": True,
            "audit_verified": True,
            "terminal_state": "review_ready",
        },
        "submission_id": "0" * 64,
        "physical_submission_count": 1,
        "logical_submission_count": 1,
        "packet_sha256": "1" * 64,
    }


def _quality_review_card(
    *, repo: Path, workspace_path: Path, workspace_home: Path, receipt: dict
) -> dict:
    return {
        "task_id": QR_TASK_ID,
        "runner": RUNNER,
        "topic": "quality_review",
        "status": "review",
        "worker_status": "review",
        "claimed_by": RUNNER,
        "read_only": True,
        "allowed_writes": [],
        "required_outputs": [],
        "claim_epoch": 4,
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {
                    "request_id": QR_REQUEST_ID,
                    "task_id": QR_TASK_ID,
                    "runner": RUNNER,
                    "topic": "quality_review",
                },
                "changed_paths": [],
                "changed_path_hashes": {},
                "workspace": {
                    "request_id": QR_REQUEST_ID,
                    "repo": str(repo),
                    "path": str(workspace_path),
                    "home": str(workspace_home),
                    "base_oid": BASE_OID,
                },
                "attempt_artifact_manifest": _manifest(),
                "evidence_record": {"reference": EVIDENCE_REFERENCE},
                "quality_review_receipt": receipt,
            },
        },
    }


@pytest.fixture
def _readonly_quality_review_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    process_dir = tmp_path / "process_dir"
    process_dir.mkdir()
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    workspace_home = tmp_path / "workspace_home"
    workspace_home.mkdir()

    receipt = _quality_review_receipt()
    card = _quality_review_card(
        repo=tmp_path / "repo",
        workspace_path=workspace_path,
        workspace_home=workspace_home,
        receipt=receipt,
    )
    repo = _seed_repo(tmp_path, card, task_id=QR_TASK_ID, topic="quality_review")
    assert card["terminal_review"]["evidence"]["workspace"]["repo"] == str(repo)

    metadata_path = process_dir / f"{QR_REQUEST_ID}.metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "request_id": QR_REQUEST_ID,
                "task_id": QR_TASK_ID,
                "runner": RUNNER,
                "topic": "quality_review",
                "workspace": {
                    "request_id": QR_REQUEST_ID,
                    "repo": str(repo),
                    "path": str(workspace_path),
                    "home": str(workspace_home),
                },
            }
        ),
        encoding="utf-8",
    )

    latest_event = {
        "task_id": QR_TASK_ID,
        "runner": RUNNER,
        "topic": "quality_review",
        "metadata_path": str(metadata_path),
        "adapter_id": "claude_cli",
    }
    manager = _StubManager(
        repo=repo, process_dir=process_dir, card=card, latest_event=latest_event
    )

    monkeypatch.setattr(process_launcher, "WorkerWorkspace", _FakeWorkerWorkspace)
    monkeypatch.setattr(process_launcher, "assert_gc_safe_workspace_shape", lambda *a, **k: None)
    monkeypatch.setattr(process_launcher, "enforce_scope", lambda *a, **k: [])
    monkeypatch.setattr(
        process_launcher,
        "_worker_workspace",
        SimpleNamespace(finalization_git_timeout_seconds=lambda: 5.0),
    )
    monkeypatch.setattr(process_launcher, "cleanup_workspace", lambda *a, **k: None)
    monkeypatch.setattr(process_launcher, "_run_declared_validations", lambda *a, **k: [])
    monkeypatch.setattr(process_launcher, "validate_required_outputs", lambda *a, **k: [])
    monkeypatch.setattr(process_launcher, "evidence_levels", _fake_evidence_levels())
    # Quality-review receipt authentication (packet audit, independence rung,
    # reviewer-boundary schema) is a collaborator unrelated to the guard under
    # test here -- the accepted-outcome receipt binding -- so it is replaced
    # with the exact sealed receipt, the same way 27 other test files replace
    # collaborators outside their guard via this seam mechanism.
    monkeypatch.setattr(
        process_launcher,
        "_verified_quality_review_receipt",
        lambda metadata, workspace, request_id: receipt,
    )
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")

    return manager, receipt


def test_readonly_quality_review_accept_review_binds_accepted_outcome_receipt(
    _readonly_quality_review_fixture,
) -> None:
    manager, _receipt = _readonly_quality_review_fixture

    result = process_launcher.ProcessManager.accept_review(manager, QR_REQUEST_ID, QR_TASK_ID)

    assert result["ok"] is True, result
    assert result["promoted_paths"] == []

    receipt = result["accepted_outcome_receipt"]
    assert receipt["schema_id"] == task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA
    assert receipt["task_id"] == QR_TASK_ID
    assert receipt["request_id"] == QR_REQUEST_ID
    assert receipt["claim_epoch"] == 4
    assert receipt["base_oid"] == BASE_OID
    assert receipt["promoted_paths"] == []
    assert receipt["changed_path_hashes"] == {}
    assert receipt["repository_revision"].startswith("sha256:")
    assert receipt["receipt_id"].startswith("sha256:")

    # The real TaskEngine guard actually ran and finished the task.
    finished_card = task_store.get_task(manager.repo, QR_TASK_ID)
    assert finished_card is not None
    assert finished_card["status"] == "finished"
    assert finished_card["accepted_request_id"] == QR_REQUEST_ID
    assert finished_card["accept_evidence"]["accepted_outcome_receipt"] == receipt


def test_readonly_quality_review_accept_review_fails_closed_without_receipt(
    _readonly_quality_review_fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the exact pre-fix regression for the quality-review branch: dropping
    the receipt kwarg must fail closed through the real unmodified TaskEngine
    guard -- proving the guard itself was never relaxed."""
    manager, _receipt = _readonly_quality_review_fixture
    real_accept_review = task_engine.accept_review

    def _accept_review_without_receipt(*args, **kwargs):
        kwargs.pop("accepted_outcome_receipt", None)
        return real_accept_review(*args, **kwargs)

    monkeypatch.setattr(process_launcher, "task_engine", SimpleNamespace(
        accept_review=_accept_review_without_receipt,
        disposition_reviewer_children=task_engine.disposition_reviewer_children,
    ))

    result = process_launcher.ProcessManager.accept_review(manager, QR_REQUEST_ID, QR_TASK_ID)

    assert result["ok"] is False
    assert result["error"] == "quality_review_finalize_failed:accepted_outcome_receipt_missing"


def test_readonly_quality_review_accept_review_fails_closed_on_tampered_receipt(
    _readonly_quality_review_fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A receipt present but not matching the sealed evidence must still fail
    closed through the real, unmodified ``_validate_accepted_outcome_receipt``
    guard -- this is not a guard relaxation, the receipt itself is tampered
    after construction and before the call into TaskEngine."""
    manager, _receipt = _readonly_quality_review_fixture
    real_accept_review = task_engine.accept_review

    def _accept_review_with_tampered_receipt(*args, **kwargs):
        tampered = dict(kwargs.get("accepted_outcome_receipt") or {})
        tampered["base_oid"] = "tampered-oid"
        kwargs["accepted_outcome_receipt"] = tampered
        return real_accept_review(*args, **kwargs)

    monkeypatch.setattr(process_launcher, "task_engine", SimpleNamespace(
        accept_review=_accept_review_with_tampered_receipt,
        disposition_reviewer_children=task_engine.disposition_reviewer_children,
    ))

    result = process_launcher.ProcessManager.accept_review(manager, QR_REQUEST_ID, QR_TASK_ID)

    assert result["ok"] is False
    assert result["error"] == (
        "quality_review_finalize_failed:accepted_outcome_receipt_identity_mismatch"
    )
