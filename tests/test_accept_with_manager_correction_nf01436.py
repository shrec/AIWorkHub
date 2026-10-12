"""NF-2026-01436: accept the sealed candidate PLUS a manager correction.

Measured 2026-10-09: needfix-NF-2026-01334-r1 (request 515c092b) carried ONE
confirmed LOW correctness finding -- a duplicate test def --
``aiworkhub_agent_accept_review`` answered ``refinement_required`` with
``blocking_severity`` false and ``disposition`` confirmed, and the only
available next step was a full reject plus a rework round plus two more
high-risk reviewers. The corrected work was then archived instead of accepted,
which is why the consumer seat measured ~54% learning-commit coverage.

These tests drive the real ``process_launcher_accept_review.accept_review``
against a real ``task_store`` repository and the real, unmodified
``task_engine.accept_review`` guard, so the accepted-outcome receipt is
validated by production code rather than asserted about:

* a confirmed low finding plus a valid patch accepts, the PROMOTED file holds
  the corrected bytes, and ``receipt.manager_correction`` binds the patch digest
  with matching candidate and corrected hashes;
* the identical request without ``manager_correction`` still refuses with
  ``refinement_required`` for exactly that finding;
* a patch outside ``allowed_writes``, a blocking or unconfirmed finding, and a
  patch that does not apply each refuse with ``manager_correction_invalid`` and
  promote nothing;
* ``task_engine._validate_accepted_outcome_receipt`` accepts a receipt without
  the optional field unchanged, accepts a well-formed one, and refuses a
  malformed one.

Collaborators unrelated to the correction (workspace shape, git scope, declared
validations, the mechanical quality gate, reviewer receipt verification) are
replaced through the documented ``process_launcher`` seam mechanism, the same
mechanism 27 other test files already rely on. Nothing about patch parsing,
scope, finding confirmation, hashing or receipt validation is mocked.
"""

from __future__ import annotations

import hashlib
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

from aiworkhub import (  # noqa: E402
    evidence_instruments,
    learning_commit_store,
    needfix_store,
    process_launcher,
    process_launcher_accept_review,
    process_launcher_acceptance,
    quality_evidence,
    task_engine,
    task_store,
)

REQUEST_ID = "req-nf01436-correction"
TASK_ID = "TASK_NF01436"
RUNNER = "claude_opus-5"
TOPIC = "code"
BASE_OID = "base-oid-nf01436"
EVIDENCE_REFERENCE = "file:///nf01436/evidence"
CLAIM_EPOCH = 7

QR_TASK_ID = "QR_NF01436_CORRECTNESS"
QR_REQUEST_ID = "rq-nf01436-correctness"
PACKET_SHA = "d" * 64
# The two digests ``bound_finding_dispositions`` binds a stored row to. Both are
# produced by ``review_orchestrator`` in production; pinning the two module-level
# helpers that compute them keeps THIS test about the correction rather than
# about canonical-digest construction.
CANDIDATE_SHA = "c" * 64
RECEIPT_SHA = "e" * 64

TARGET = "src/demo/widget.py"
OUTSIDE = "src/other/forbidden.py"
FINDING_ID = f"reviewer:{quality_evidence.LENS_CORRECTNESS}:dup-test-def"
BLOCKING_FINDING_ID = f"reviewer:{quality_evidence.LENS_CORRECTNESS}:unsafe-write"
BLOCKING_SEVERITY = sorted(quality_evidence.BLOCKING_SEVERITIES)[0]

# The exact defect NF-2026-01334-r1 was rejected for: one duplicate test def.
CANDIDATE = (
    "def widget():\n"
    "    return 1\n"
    "\n"
    "\n"
    "def test_widget():\n"
    "    assert widget() == 1\n"
    "\n"
    "\n"
    "def test_widget():\n"
    "    assert widget() == 1\n"
)
CORRECTED = (
    "def widget():\n"
    "    return 1\n"
    "\n"
    "\n"
    "def test_widget():\n"
    "    assert widget() == 1\n"
)
CANDIDATE_DIGEST = hashlib.sha256(CANDIDATE.encode("utf-8")).hexdigest()
CORRECTED_DIGEST = hashlib.sha256(CORRECTED.encode("utf-8")).hexdigest()


def _patch(path: str = TARGET, *, context: str = "    assert widget() == 1") -> str:
    """One unified diff that drops the duplicated test def from ``path``."""
    return (
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -6,5 +6,1 @@\n"
        f" {context}\n"
        "-\n"
        "-\n"
        "-def test_widget():\n"
        "-    assert widget() == 1\n"
    )


def _low_finding() -> dict:
    return {
        "id": "dup-test-def",
        "lens": quality_evidence.LENS_CORRECTNESS,
        "severity": "low",
        "disposition": "defect",
        "title": "duplicate test def shadows the first",
        "detail": "test_widget is defined twice; the first body never runs.",
    }


def _blocking_finding() -> dict:
    return {
        "id": "unsafe-write",
        "lens": quality_evidence.LENS_CORRECTNESS,
        "severity": BLOCKING_SEVERITY,
        "disposition": "defect",
        "title": "unguarded write",
        "detail": "writes outside the declared scope.",
    }


def _disposition(finding_id: str = FINDING_ID, *, disposition: str = "confirmed") -> dict:
    return {
        "finding_id": finding_id,
        "reviewer_request_id": QR_REQUEST_ID,
        "candidate_sha256": CANDIDATE_SHA,
        "reviewer_receipt_sha256": RECEIPT_SHA,
        "disposition": disposition,
        "receipt_sha256": "f" * 64,
    }


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


def _fake_quality_evidence(
    *,
    passed: bool = True,
    blocking_checks: list[str] | None = None,
    recorded_calls: list[dict] | None = None,
) -> SimpleNamespace:
    """The mechanical gate, neutralised; the lens census is kept real.

    ``required_reviewer_lenses`` still demands the correctness lens, which is
    what makes the reviewer selection, the finding fold and the lift behave as
    they do in production. ``combined_tree_required`` is False so the test does
    not pay for a union workspace it asserts nothing about.

    ``passed``/``blocking_checks`` let a caller simulate the gate refusing the
    corrected candidate (NF-2026-01469 rollback coverage); ``recorded_calls``,
    when given, collects every call's keyword arguments so a test can prove
    exactly what ``accept_review`` passed the gate.
    """

    def _run_completion_quality_gate(*args: object, **kwargs: object) -> dict:
        if recorded_calls is not None:
            recorded_calls.append(dict(kwargs))
        return {
            "schema_id": "aiworkhub.completion_quality_gate.v1",
            "applicable": True,
            "passed": passed,
            "checks": [],
            "blocking_checks": list(blocking_checks or []),
        }

    return SimpleNamespace(
        STATUS_FAILED=quality_evidence.STATUS_FAILED,
        STATUS_PASSED=quality_evidence.STATUS_PASSED,
        MAX_REVIEW_REPORTS=quality_evidence.MAX_REVIEW_REPORTS,
        run_destructive_diff_checks=lambda *a, **k: [],
        derive_risk_signals=lambda *a, **k: [],
        assess_quality_policy_authority=lambda *a, **k: {
            "weakened": False,
            "escalation_signal": "",
            "candidate_policy_source": "candidate_declared",
            "canonical_declared_checks": [],
            "canonical_config_readable": True,
        },
        resolve_risk_profile=lambda *a, **k: {
            "effective_tier": "low",
            "required_reviewer_lenses": [quality_evidence.LENS_CORRECTNESS],
            "combined_tree_required": False,
            "explicit_human_approval_required": False,
        },
        run_completion_quality_gate=_run_completion_quality_gate,
    )


def _manifest() -> dict:
    return {
        "schema_id": "aiworkhub.attempt_artifact_bundle_receipt.v1",
        "attempt_id": REQUEST_ID,
        "verified": True,
        "manifest_path": "/does/not/matter",
        "manifest_sha256": "0" * 64,
    }


def _target_card(*, repo: Path, workspace_path: Path, workspace_home: Path) -> dict:
    return {
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": TOPIC,
        "status": "review",
        "worker_status": "review",
        "claimed_by": RUNNER,
        "claim_epoch": CLAIM_EPOCH,
        "risk_tier": "low",
        "allowed_writes": [TARGET],
        "required_outputs": [],
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {
                    "request_id": REQUEST_ID,
                    "task_id": TASK_ID,
                    "runner": RUNNER,
                    "topic": TOPIC,
                },
                "changed_paths": [TARGET],
                "changed_path_hashes": {TARGET: CANDIDATE_DIGEST},
                "required_outputs": [],
                "workspace": {
                    "request_id": REQUEST_ID,
                    "repo": str(repo),
                    "path": str(workspace_path),
                    "home": str(workspace_home),
                    "base_oid": BASE_OID,
                },
                "attempt_artifact_manifest": _manifest(),
                "evidence_record": {"reference": EVIDENCE_REFERENCE},
            },
        },
    }


def _reviewer_card(findings: list[dict]) -> dict:
    return {
        "task_id": QR_TASK_ID,
        "topic": "quality_review",
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "quality_review": {
                    "lens": quality_evidence.LENS_CORRECTNESS,
                    "packet_sha256": PACKET_SHA,
                    "target_task_id": TASK_ID,
                    "target_request_id": REQUEST_ID,
                },
                "quality_review_receipt": {
                    "packet_sha256": PACKET_SHA,
                    "report": {
                        "lens": quality_evidence.LENS_CORRECTNESS,
                        "read_only": True,
                        "can_mutate_repo": False,
                        "findings": list(findings),
                    },
                },
            },
        },
    }


class _StubManager:
    """The manager collaborators the writable accept path actually calls."""

    def __init__(
        self,
        *,
        repo: Path,
        process_dir: Path,
        cards: dict[str, dict],
        events: dict[str, list[dict]],
        dispositions: list[dict],
    ) -> None:
        self.repo = repo
        self.process_dir = process_dir
        self.process_log_path = repo / "process.log"
        self.cards = cards
        self.events = events
        self.dispositions = dispositions
        self.retention_events: list[tuple[dict, str]] = []
        self.promoted: list[list[str]] = []

    # --- identity / ledger -------------------------------------------------
    def _request_events(self, request_id: str) -> list[dict]:
        return list(self.events.get(request_id) or [])

    def _latest_by_request(self) -> dict[str, dict]:
        return {
            request_id: rows[-1]
            for request_id, rows in self.events.items()
            if rows and request_id != REQUEST_ID
        }

    def _show_task(self, task_id: str) -> dict:
        return {"returncode": 0, "stdout": json.dumps(self.cards[task_id])}

    @staticmethod
    def _metadata_from_events(events: list[dict]) -> Path | None:
        for event in reversed(events):
            raw = event.get("metadata_path")
            if raw:
                return Path(str(raw))
        return None

    # --- locks / evidence --------------------------------------------------
    def _request_lock(self, request_id: str):
        return nullcontext()

    def _promotion_lock(self):
        return nullcontext()

    def _context_write_intent_snapshot(self, request_id: str) -> dict:
        return {"ok": False}

    def _verify_attempt_artifact_receipt(self, request_id: str, raw: object) -> dict:
        assert isinstance(raw, dict)
        return dict(raw)

    def _minimum_acceptance_evidence_level(self, card, **_kwargs):
        return "min"

    def _attempt_evidence_reference(self, request_id: str, receipt: dict) -> str:
        return EVIDENCE_REFERENCE

    def _canonical_outcome_evidence(
        self, request_id, receipt, *, level, verified_by, message
    ) -> dict:
        return {
            "reference": EVIDENCE_REFERENCE,
            "level": str(level),
            "verified_by": verified_by,
            "message": message,
        }

    def _candidate_reachability_inputs(self, workspace, changed) -> dict:
        return {}

    def review_finding_dispositions(self, task_id: str, request_id: str) -> list[dict]:
        return [dict(row) for row in self.dispositions]

    # --- promotion ---------------------------------------------------------
    def _promote_accepted_candidate(
        self, workspace, changed: list[str], merged_hashes=None
    ) -> list[str]:
        promoted: list[str] = []
        for relative in sorted(changed):
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((workspace.path / relative).read_bytes())
            promoted.append(relative)
        self.promoted.append(promoted)
        return promoted

    def _close_accepted_task_needfix(self, task_id: str, request_id: str) -> dict:
        return {"state": "not_attempted"}

    def _retention_event(self, payload: dict, *, disposition: str) -> None:
        self.retention_events.append((payload, disposition))


def _seed_repo(tmp_path: Path, cards: list[tuple[dict, str, str]]) -> Path:
    """One real task store holding the target card and its reviewer child."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-10-09T00:00:00+00:00"
    conn = sqlite3.connect(db_path)
    try:
        for card, runner, topic in cards:
            conn.execute(
                "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
                "priority, objective, card_json, created_at, updated_at, claimed_by, "
                "claimed_at, started_at, origin_thread_id) "
                "VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?, ?)",
                (
                    card["task_id"], runner, topic, "review", "review",
                    json.dumps(card), now, now, runner, now, now, "thread-nf01436",
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return repo.resolve()


def _accept_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    findings: list[dict] | None = None,
    dispositions: list[dict] | None = None,
    gate_passed: bool = True,
    gate_blocking_checks: list[str] | None = None,
    gate_calls: list[dict] | None = None,
) -> _StubManager:
    workspace_path = tmp_path / "workspace"
    (workspace_path / "src" / "demo").mkdir(parents=True)
    (workspace_path / TARGET).write_bytes(CANDIDATE.encode("utf-8"))
    workspace_home = tmp_path / "workspace_home"
    workspace_home.mkdir()
    process_dir = tmp_path / "process_dir"
    process_dir.mkdir()

    reviewer_card = _reviewer_card(findings if findings is not None else [_low_finding()])
    target_card = _target_card(
        repo=(tmp_path / "repo").resolve(),
        workspace_path=workspace_path,
        workspace_home=workspace_home,
    )
    repo = _seed_repo(
        tmp_path,
        [(target_card, RUNNER, TOPIC), (reviewer_card, "reviewer", "quality_review")],
    )
    assert target_card["terminal_review"]["evidence"]["workspace"]["repo"] == str(repo)

    manager = _StubManager(
        repo=repo,
        process_dir=process_dir,
        cards={TASK_ID: target_card, QR_TASK_ID: reviewer_card},
        events={
            REQUEST_ID: [
                {
                    "request_id": REQUEST_ID,
                    "task_id": TASK_ID,
                    "runner": RUNNER,
                    "topic": TOPIC,
                    "adapter_id": "claude_cli",
                    "state": "review_ready",
                }
            ],
            QR_REQUEST_ID: [
                {
                    "request_id": QR_REQUEST_ID,
                    "task_id": QR_TASK_ID,
                    "state": "accepted",
                    "finished_at": "2026-10-09T01:00:00+00:00",
                }
            ],
        },
        dispositions=(
            dispositions if dispositions is not None else [_disposition()]
        ),
    )

    receipt = reviewer_card["terminal_review"]["evidence"]["quality_review_receipt"]
    # ``core`` is replaced wholesale below, but the accept path still reads one
    # real helper through it: ``_canonical_task_status`` calls
    # ``core._lifecycle_state(card)``.  Capture it from the live module first so
    # the stand-in answers that call instead of raising AttributeError.
    real_lifecycle_state = process_launcher.core._lifecycle_state
    core = SimpleNamespace(
        writes_allowed=lambda: True,
        manager_bootstrap=lambda: {
            "role": "manager",
            "repo": str(repo),
            "manager_route": {"provider": "claude", "thread_id": "sess-nf01436"},
        },
        _manager_chat_seat_identity=lambda: {
            "provider": "claude", "session_id": "sess-nf01436",
        },
        _claude_manager_identity=lambda: {},
        _codex_manager_identity=lambda: {},
        _lifecycle_state=real_lifecycle_state,
    )

    monkeypatch.setattr(process_launcher, "WorkerWorkspace", _FakeWorkerWorkspace)
    monkeypatch.setattr(
        process_launcher, "assert_gc_safe_workspace_shape", lambda *a, **k: None
    )
    monkeypatch.setattr(process_launcher, "enforce_scope", lambda *a, **k: [TARGET])
    monkeypatch.setattr(
        process_launcher,
        "_worker_workspace",
        SimpleNamespace(finalization_git_timeout_seconds=lambda: 5.0),
    )
    monkeypatch.setattr(process_launcher, "cleanup_workspace", lambda *a, **k: None)
    monkeypatch.setattr(process_launcher, "validate_required_outputs", lambda *a, **k: [])
    monkeypatch.setattr(process_launcher, "_run_declared_validations", lambda *a, **k: [])
    monkeypatch.setattr(
        process_launcher,
        "_run_full_snapshot_validations",
        lambda *a, **k: ([], {"combined_tree": {}}),
    )
    monkeypatch.setattr(process_launcher, "_enforce_behavioral_gate", lambda *a, **k: None)
    monkeypatch.setattr(process_launcher, "evidence_levels", _fake_evidence_levels())
    monkeypatch.setattr(
        process_launcher,
        "quality_evidence",
        _fake_quality_evidence(
            passed=gate_passed,
            blocking_checks=gate_blocking_checks,
            recorded_calls=gate_calls,
        ),
    )
    monkeypatch.setattr(process_launcher, "core", core)
    monkeypatch.setattr(
        process_launcher,
        "learning_commit",
        SimpleNamespace(commit_owed=lambda **kwargs: {"owed": True}),
    )
    monkeypatch.setattr(
        process_launcher,
        "_verified_accepted_quality_review_receipt",
        lambda *a, **k: receipt,
    )
    # The two digests a stored disposition is bound to; see CANDIDATE_SHA above.
    monkeypatch.setattr(
        process_launcher_accept_review, "_target_candidate_sha256",
        lambda card: CANDIDATE_SHA,
    )
    monkeypatch.setattr(
        process_launcher_accept_review, "_receipt_sha256", lambda value: RECEIPT_SHA
    )
    monkeypatch.setattr(
        process_launcher_accept_review,
        "manager_skill_tools",
        SimpleNamespace(record_decision_evidence=lambda *a, **k: None),
    )
    # Gates with their own cards; each is asserted elsewhere and would only add
    # unrelated failure modes here.
    monkeypatch.setattr(
        evidence_instruments,
        "review_evidence_audit",
        lambda *a, **k: {"blocking": False, "blockers": []},
    )
    monkeypatch.setattr(
        learning_commit_store, "record_decision_event", lambda *a, **k: {"ok": True}
    )
    monkeypatch.setattr(
        needfix_store, "draft_from_review_evidence", lambda *a, **k: []
    )
    monkeypatch.setattr(
        task_engine,
        "disposition_reviewer_children",
        lambda *a, **k: {"ok": True, "stdout": json.dumps({"finalized": [QR_TASK_ID]})},
    )
    return manager


def _accept(manager: _StubManager, **kwargs) -> dict:
    return process_launcher_accept_review.accept_review(
        manager, REQUEST_ID, TASK_ID, **kwargs
    )


# ---------------------------------------------------------------------------
# (a) the measured case: one confirmed low finding plus a valid patch accepts.
# ---------------------------------------------------------------------------
def test_confirmed_low_finding_plus_valid_patch_accepts_the_corrected_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _accept_env(tmp_path, monkeypatch)

    result = _accept(
        manager,
        manager_correction={
            "patch": _patch(),
            "finding_ids": [FINDING_ID],
            "reason": "dropped the duplicated test def the reviewer confirmed",
        },
    )

    assert "error" not in result
    assert result["ok"] is True, result.get("error")
    assert result["promoted_paths"] == [TARGET]
    # The bytes that landed in the canonical repo are the CORRECTED bytes, not
    # the sealed candidate's -- the whole point of the flow.
    assert (manager.repo / TARGET).read_text(encoding="utf-8") == CORRECTED

    receipt = result["accepted_outcome_receipt"]
    assert receipt["changed_path_hashes"] == {TARGET: CORRECTED_DIGEST}
    correction = receipt["manager_correction"]
    assert set(correction) == {
        "patch_sha256", "finding_ids", "paths",
        "candidate_path_hashes", "corrected_path_hashes", "reason",
    }
    assert correction["patch_sha256"] == hashlib.sha256(
        _patch().encode("utf-8")
    ).hexdigest()
    assert correction["finding_ids"] == [FINDING_ID]
    assert correction["paths"] == [TARGET]
    assert correction["candidate_path_hashes"] == {TARGET: CANDIDATE_DIGEST}
    assert correction["corrected_path_hashes"] == {TARGET: CORRECTED_DIGEST}
    assert "duplicated test def" in correction["reason"]

    # The real ``task_engine.accept_review`` guard validated and persisted it,
    # so ``aiworkhub_manager_learning_commit outcome=accepted`` has a finished
    # card with a bound receipt to read.
    card = task_store.get_task(manager.repo, TASK_ID)
    assert card["status"] == "finished"
    assert card["accepted_request_id"] == REQUEST_ID
    assert (
        card["accept_evidence"]["accepted_outcome_receipt"]["manager_correction"]
        == correction
    )


# ---------------------------------------------------------------------------
# (b) without the correction, the refusal is byte-identical to today.
# ---------------------------------------------------------------------------
def test_without_manager_correction_the_confirmed_finding_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _accept_env(tmp_path, monkeypatch)

    result = _accept(manager)

    assert result["ok"] is False
    assert result["error"] == f"refinement_required:{FINDING_ID}"
    blocker = result["accept_blockers"][0]
    assert blocker["kind"] == "refinement_required"
    assert blocker["blocking_severity"] is False
    assert blocker["disposition"] == "confirmed"
    assert manager.promoted == []
    assert not (manager.repo / TARGET).exists()


# ---------------------------------------------------------------------------
# (c)-(e) every invalid correction refuses by name and promotes nothing.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "correction, findings, dispositions, expected",
    [
        pytest.param(
            {"patch": _patch(OUTSIDE), "finding_ids": [FINDING_ID]},
            None,
            None,
            f"manager_correction_invalid:path_outside_allowed_writes:{OUTSIDE}",
            id="path_outside_allowed_writes",
        ),
        pytest.param(
            {"patch": _patch(), "finding_ids": [BLOCKING_FINDING_ID]},
            [_low_finding(), _blocking_finding()],
            [_disposition(), _disposition(BLOCKING_FINDING_ID)],
            f"manager_correction_invalid:finding_blocking_severity:{BLOCKING_FINDING_ID}",
            id="blocking_severity_is_never_correctable",
        ),
        pytest.param(
            {"patch": _patch(), "finding_ids": [FINDING_ID]},
            None,
            [],
            f"manager_correction_invalid:finding_not_confirmed:{FINDING_ID}",
            id="finding_without_a_confirmed_disposition",
        ),
        pytest.param(
            {"patch": _patch(), "finding_ids": [FINDING_ID]},
            None,
            [_disposition(disposition="dismissed")],
            f"manager_correction_invalid:finding_not_confirmed:{FINDING_ID}",
            id="dismissed_is_not_confirmed",
        ),
        pytest.param(
            {"patch": _patch(context="    assert widget() == 2"), "finding_ids": [FINDING_ID]},
            None,
            None,
            "manager_correction_invalid:patch_context_mismatch",
            id="patch_does_not_apply",
        ),
        pytest.param(
            {"patch": _patch(), "finding_ids": []},
            None,
            None,
            "manager_correction_invalid:finding_ids_empty",
            id="no_finding_named",
        ),
        pytest.param(
            {"patch": "not a diff at all\n", "finding_ids": [FINDING_ID]},
            None,
            None,
            "manager_correction_invalid:patch_unparsable",
            id="patch_does_not_parse",
        ),
    ],
)
def test_invalid_manager_correction_refuses_before_any_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    correction: dict,
    findings: list[dict] | None,
    dispositions: list[dict] | None,
    expected: str,
) -> None:
    manager = _accept_env(
        tmp_path, monkeypatch, findings=findings, dispositions=dispositions
    )

    result = _accept(manager, manager_correction=correction)

    assert result["ok"] is False
    assert result["error"] == expected
    assert result["promoted_paths"] == []
    # Nothing promoted, and the sealed candidate in the accept tree is untouched.
    assert manager.promoted == []
    assert not (manager.repo / TARGET).exists()
    workspace_path = Path(
        manager.cards[TASK_ID]["terminal_review"]["evidence"]["workspace"]["path"]
    )
    assert (workspace_path / TARGET).read_text(encoding="utf-8") == CANDIDATE
    assert task_store.get_task(manager.repo, TASK_ID)["status"] == "review"


def test_an_unverified_manager_cannot_correct_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The correction is a manager WRITE, so an unverified route refuses it."""
    manager = _accept_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        process_launcher,
        "core",
        SimpleNamespace(
            writes_allowed=lambda: True,
            manager_bootstrap=lambda: {"role": "worker"},
            _manager_chat_seat_identity=lambda: {},
            _claude_manager_identity=lambda: {},
            _codex_manager_identity=lambda: {},
            # Only the identity changes here; the lifecycle reader the accept
            # path calls through ``_canonical_task_status`` stays the real one
            # the fixture above installed.
            _lifecycle_state=process_launcher.core._lifecycle_state,
        ),
    )

    result = _accept(
        manager,
        manager_correction={"patch": _patch(), "finding_ids": [FINDING_ID]},
    )

    assert result["error"] == "manager_correction_invalid:manager_identity_unverified"
    assert manager.promoted == []


# ---------------------------------------------------------------------------
# The lift itself, read directly off ``_refinement_blockers``.
# ---------------------------------------------------------------------------
def _reviewer_row(findings: list[dict]) -> dict:
    return {
        "task_id": QR_TASK_ID,
        "request_id": QR_REQUEST_ID,
        "lens": quality_evidence.LENS_CORRECTNESS,
        "state": "accepted",
        "usable": True,
        "receipt": {
            "report": {
                "lens": quality_evidence.LENS_CORRECTNESS,
                "findings": list(findings),
            }
        },
    }


def test_refinement_blockers_lifts_only_the_corrected_confirmed_finding() -> None:
    rows: list[dict] = []
    blockers = process_launcher_accept_review._refinement_blockers(
        [_reviewer_row([_low_finding(), _blocking_finding()])],
        finding_dispositions=[_disposition(), _disposition(BLOCKING_FINDING_ID)],
        disposition_rows=rows,
        manager_correction={"finding_ids": [FINDING_ID, BLOCKING_FINDING_ID]},
    )

    # The blocking finding is never lifted, however it is disposed or corrected.
    assert [row["error"] for row in blockers] == [
        f"refinement_required:{BLOCKING_FINDING_ID}"
    ]
    assert blockers[0]["blocking_severity"] is True
    by_id = {row["finding_id"]: row for row in rows}
    assert by_id[FINDING_ID]["blocker_lifted"] is True
    assert by_id[FINDING_ID]["lifted_by"] == "manager_correction"
    assert by_id[BLOCKING_FINDING_ID]["blocker_lifted"] is False
    assert "lifted_by" not in by_id[BLOCKING_FINDING_ID]


def test_refinement_blockers_without_a_correction_is_unchanged() -> None:
    rows: list[dict] = []
    blockers = process_launcher_accept_review._refinement_blockers(
        [_reviewer_row([_low_finding()])],
        finding_dispositions=[_disposition()],
        disposition_rows=rows,
    )

    assert [row["error"] for row in blockers] == [f"refinement_required:{FINDING_ID}"]
    assert rows[0]["blocker_lifted"] is False
    assert "lifted_by" not in rows[0]


# ---------------------------------------------------------------------------
# The applier: exact, or it refuses.
# ---------------------------------------------------------------------------
def test_corrected_candidate_texts_applies_exactly_and_refuses_fuzz() -> None:
    assert process_launcher_accept_review.corrected_candidate_texts(
        _patch(), lambda relative: CANDIDATE
    ) == {TARGET: CORRECTED}

    for patch_text, reason in (
        (_patch(context="    assert widget() == 2"), "patch_context_mismatch"),
        (_patch() + _patch(), "patch_duplicate_file"),
        (f"--- a/{TARGET}\n+++ b/{TARGET}\n", "patch_no_hunks"),
        (f"--- a/{TARGET}\n+++ /dev/null\n", "patch_file_create_or_delete_unsupported"),
        (f"--- a/{TARGET}\n+++ b/other.py\n@@ -1,1 +1,1 @@\n", "patch_rename_unsupported"),
        ("--- a/../escape.py\n+++ b/../escape.py\n", "patch_path_invalid"),
        ("--- a/C:/Windows/escape.py\n+++ b/C:/Windows/escape.py\n", "patch_path_invalid"),
        ("--- a/x.py:stream\n+++ b/x.py:stream\n", "patch_path_invalid"),
    ):
        with pytest.raises(
            process_launcher_accept_review.ManagerCorrectionError
        ) as excinfo:
            process_launcher_accept_review.corrected_candidate_texts(
                patch_text, lambda relative: CANDIDATE
            )
        assert str(excinfo.value) == reason


# ---------------------------------------------------------------------------
# (f) ``_validate_accepted_outcome_receipt``: absent, present, malformed.
# ---------------------------------------------------------------------------
def _receipt_card(repo: Path) -> dict:
    return {
        "task_id": TASK_ID,
        "claim_epoch": CLAIM_EPOCH,
        "terminal_review": {
            "evidence": {
                "changed_paths": [TARGET],
                "changed_path_hashes": {TARGET: CANDIDATE_DIGEST},
                "attempt_artifact_manifest": _manifest(),
                "workspace": {"base_oid": BASE_OID},
            }
        },
    }


def _promoted_repo(tmp_path: Path, name: str, content: str) -> Path:
    repo = tmp_path / name
    (repo / "src" / "demo").mkdir(parents=True)
    # Bytes, not text: the digests under test are over exact bytes, so no
    # platform may translate a newline on the way in.
    (repo / TARGET).write_bytes(content.encode("utf-8"))
    return repo


def _resign(receipt: dict) -> dict:
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_id"}
    return {
        **unsigned,
        "receipt_id": "sha256:" + task_engine._canonical_json_hash(unsigned),
    }


def test_validate_accepted_outcome_receipt_accepts_a_receipt_without_the_field(
    tmp_path: Path,
) -> None:
    repo = _promoted_repo(tmp_path, "plain", CANDIDATE)
    receipt = process_launcher_acceptance.accepted_outcome_receipt(
        repo,
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        claim_epoch=CLAIM_EPOCH,
        base_oid=BASE_OID,
        promoted_paths=[TARGET],
        changed_path_hashes={TARGET: CANDIDATE_DIGEST},
        attempt_artifact_manifest=_manifest(),
    )
    assert "manager_correction" not in receipt

    validated, error = task_engine._validate_accepted_outcome_receipt(
        repo, _receipt_card(repo), TASK_ID, REQUEST_ID, receipt
    )

    assert error == ""
    assert validated == receipt


def _corrected_receipt(repo: Path, **overrides) -> dict:
    correction = {
        "patch_sha256": hashlib.sha256(_patch().encode("utf-8")).hexdigest(),
        "finding_ids": [FINDING_ID],
        "paths": [TARGET],
        "candidate_path_hashes": {TARGET: CANDIDATE_DIGEST},
        "corrected_path_hashes": {TARGET: CORRECTED_DIGEST},
        "reason": "dropped the duplicated test def",
        **overrides,
    }
    return process_launcher_acceptance.accepted_outcome_receipt(
        repo,
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        claim_epoch=CLAIM_EPOCH,
        base_oid=BASE_OID,
        promoted_paths=[TARGET],
        changed_path_hashes={TARGET: CORRECTED_DIGEST},
        attempt_artifact_manifest=_manifest(),
        manager_correction=correction,
    )


def test_validate_accepted_outcome_receipt_accepts_a_well_formed_correction(
    tmp_path: Path,
) -> None:
    repo = _promoted_repo(tmp_path, "corrected", CORRECTED)
    receipt = _corrected_receipt(repo)

    validated, error = task_engine._validate_accepted_outcome_receipt(
        repo, _receipt_card(repo), TASK_ID, REQUEST_ID, receipt
    )

    assert error == ""
    assert validated["manager_correction"]["corrected_path_hashes"] == {
        TARGET: CORRECTED_DIGEST
    }
    # The sealed candidate hash is still recoverable from the receipt, so a
    # reader can see both the bytes a reviewer judged and the bytes that landed.
    assert validated["manager_correction"]["candidate_path_hashes"] == {
        TARGET: CANDIDATE_DIGEST
    }


@pytest.mark.parametrize(
    "mutate, expected",
    [
        pytest.param(
            lambda correction: {
                key: value for key, value in correction.items() if key != "reason"
            },
            "accepted_outcome_receipt_manager_correction_malformed",
            id="missing_field",
        ),
        pytest.param(
            lambda correction: {**correction, "finding_ids": []},
            "accepted_outcome_receipt_manager_correction_malformed",
            id="no_finding_named",
        ),
        pytest.param(
            lambda correction: {
                **correction, "candidate_path_hashes": {TARGET: "a" * 64}
            },
            "accepted_outcome_receipt_manager_correction_malformed",
            id="candidate_hash_is_not_the_sealed_one",
        ),
        pytest.param(
            lambda correction: {
                **correction, "corrected_path_hashes": {TARGET: CANDIDATE_DIGEST}
            },
            "accepted_outcome_receipt_manager_correction_malformed",
            id="correction_that_changes_nothing",
        ),
        pytest.param(
            lambda correction: {**correction, "paths": ["src/demo/other.py"]},
            "accepted_outcome_receipt_manager_correction_malformed",
            id="path_the_candidate_never_changed",
        ),
        pytest.param(
            lambda correction: {**correction, "patch_sha256": "not-a-digest"},
            "accepted_outcome_receipt_manager_correction_malformed",
            id="patch_digest_is_not_a_digest",
        ),
    ],
)
def test_validate_accepted_outcome_receipt_refuses_a_malformed_correction(
    tmp_path: Path, mutate, expected: str
) -> None:
    repo = _promoted_repo(tmp_path, "tampered", CORRECTED)
    receipt = _corrected_receipt(repo)
    tampered = _resign(
        {**receipt, "manager_correction": mutate(receipt["manager_correction"])}
    )

    validated, error = task_engine._validate_accepted_outcome_receipt(
        repo, _receipt_card(repo), TASK_ID, REQUEST_ID, tampered
    )

    assert validated is None
    assert error == expected


def test_accepted_outcome_receipt_refuses_a_correction_the_repo_does_not_hold(
    tmp_path: Path,
) -> None:
    """The receipt is built AFTER promotion, so it checks the bytes that landed."""
    repo = _promoted_repo(tmp_path, "stale", CANDIDATE)

    with pytest.raises(process_launcher_acceptance.WorkspaceError) as excinfo:
        _corrected_receipt(repo)

    assert "manager_correction_receipt_mismatch" in str(
        excinfo.value
    ) or "post_promotion_candidate_mismatch" in str(excinfo.value)


# ---------------------------------------------------------------------------
# NF-2026-01469: the authoritative gate itself must honour the correction.
# ---------------------------------------------------------------------------
def _risk_profile_no_review() -> dict:
    """A profile that makes no reviewer lens required.

    The findings loop in ``fold_quality_verdict`` folds LENS_CORRECTNESS /
    LENS_SECURITY findings unconditionally -- required lenses only gate the
    independence/missing-reviewer machinery, which these tests assert
    nothing about.
    """
    return {
        "effective_tier": "low",
        "required_reviewer_lenses": [],
        "combined_tree_required": False,
        "cross_provider_required": False,
        "explicit_human_approval_required": False,
    }


def _judged_report(
    finding: dict,
    *,
    lens: str = quality_evidence.LENS_CORRECTNESS,
    provider: str = "claude",
) -> dict:
    """One normalizable reviewer report carrying exactly ``finding``."""
    return {
        "lens": lens,
        "provider": provider,
        "read_only": True,
        "can_mutate_repo": False,
        "findings": [
            {
                **finding,
                "summary": finding.get("title", "finding"),
                "evidence": finding.get("detail", "evidence"),
            }
        ],
    }


def test_fold_quality_verdict_corrected_finding_ids_lifts_a_confirmed_low_finding() -> None:
    verdict = quality_evidence.fold_quality_verdict(
        [],
        risk_profile=_risk_profile_no_review(),
        reviewer_reports=[_judged_report(_low_finding())],
        corrected_finding_ids=[FINDING_ID],
    )

    assert verdict["passed"] is True
    assert not any(
        blocker.startswith("refinement_required:")
        for blocker in verdict["blocking_evidence"]
    )
    assert verdict["manager_corrected_findings"] == [
        {
            "finding_id": FINDING_ID,
            "lens": quality_evidence.LENS_CORRECTNESS,
            "severity": "low",
            "classification": "manager_corrected_finding",
        }
    ]


def test_fold_quality_verdict_without_corrected_finding_ids_refuses_as_today() -> None:
    verdict = quality_evidence.fold_quality_verdict(
        [],
        risk_profile=_risk_profile_no_review(),
        reviewer_reports=[_judged_report(_low_finding())],
    )

    assert verdict["passed"] is False
    assert verdict["blocking_evidence"] == [f"refinement_required:{FINDING_ID}"]
    assert "manager_corrected_findings" not in verdict


def test_fold_quality_verdict_never_lifts_a_blocking_severity() -> None:
    verdict = quality_evidence.fold_quality_verdict(
        [],
        risk_profile=_risk_profile_no_review(),
        reviewer_reports=[_judged_report(_blocking_finding())],
        corrected_finding_ids=[BLOCKING_FINDING_ID],
    )

    assert verdict["passed"] is False
    assert BLOCKING_FINDING_ID in verdict["blocking_evidence"]
    assert "manager_corrected_findings" not in verdict


def test_fold_quality_verdict_does_not_lift_a_duplicated_finding_id() -> None:
    verdict = quality_evidence.fold_quality_verdict(
        [],
        risk_profile=_risk_profile_no_review(),
        reviewer_reports=[
            _judged_report(_low_finding(), provider="claude-a"),
            _judged_report(_low_finding(), provider="claude-b"),
        ],
        corrected_finding_ids=[FINDING_ID],
    )

    assert verdict["passed"] is False
    assert f"refinement_required:{FINDING_ID}" in verdict["blocking_evidence"]
    assert "manager_corrected_findings" not in verdict


def _correction_payload() -> dict:
    return {
        "patch": _patch(),
        "finding_ids": [FINDING_ID],
        "reason": "dropped the duplicated test def the reviewer confirmed",
    }


def test_accept_passes_corrected_finding_ids_to_the_quality_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    manager = _accept_env(tmp_path, monkeypatch, gate_calls=calls)

    result = _accept(manager, manager_correction=_correction_payload())

    assert result["ok"] is True, result.get("error")
    assert len(calls) == 1
    assert calls[0]["corrected_finding_ids"] == [FINDING_ID]


def test_quality_gate_failure_restores_the_retained_worktree_after_a_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _accept_env(
        tmp_path, monkeypatch, gate_passed=False, gate_blocking_checks=["x"]
    )
    workspace_path = Path(
        manager.cards[TASK_ID]["terminal_review"]["evidence"]["workspace"]["path"]
    )

    result = _accept(manager, manager_correction=_correction_payload())

    assert result["ok"] is False
    assert "quality_gate_failed" in result["error"]
    assert manager.promoted == []
    # The corrected bytes were written into the retained worktree before the
    # gate ran; a failed accept must restore the sealed CANDIDATE bytes there,
    # not leave the CORRECTED ones behind (NF-2026-01469).
    assert (workspace_path / TARGET).read_text(encoding="utf-8") == CANDIDATE


def test_behavioral_gate_workspace_error_still_restores_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _accept_env(tmp_path, monkeypatch)
    workspace_path = Path(
        manager.cards[TASK_ID]["terminal_review"]["evidence"]["workspace"]["path"]
    )

    def _raise_workspace_error(*_a, **_k):
        raise process_launcher.WorkspaceError("boom")

    monkeypatch.setattr(
        process_launcher, "_enforce_behavioral_gate", _raise_workspace_error
    )

    result = _accept(manager, manager_correction=_correction_payload())

    assert result["ok"] is False
    assert "revalidation_failed:" in result["error"]
    # The CORRECTED bytes were written before the gate ran; a WorkspaceError
    # raised by a later step must still restore the sealed CANDIDATE bytes
    # (NF-2026-01469).
    assert (workspace_path / TARGET).read_text(encoding="utf-8") == CANDIDATE


def test_behavioral_gate_unnamed_exception_still_restores_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _accept_env(tmp_path, monkeypatch)
    workspace_path = Path(
        manager.cards[TASK_ID]["terminal_review"]["evidence"]["workspace"]["path"]
    )

    def _raise_runtime_error(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        process_launcher, "_enforce_behavioral_gate", _raise_runtime_error
    )

    # An exception that is neither WorkspaceError nor GitCommandTimeout still
    # propagates -- the accept call does not swallow it -- but the retained
    # worktree must not be left holding the CORRECTED bytes (NF-2026-01469).
    with pytest.raises(RuntimeError):
        _accept(manager, manager_correction=_correction_payload())

    assert (workspace_path / TARGET).read_text(encoding="utf-8") == CANDIDATE


def test_restore_correction_originals_continues_past_a_failing_path(
    tmp_path: Path,
) -> None:
    workspace_path = tmp_path / "workspace"
    (workspace_path / "blocked_dir").mkdir(parents=True)
    (workspace_path / "ok_file.txt").write_bytes(b"corrected")
    workspace = SimpleNamespace(path=workspace_path)

    failed = process_launcher_accept_review._restore_correction_originals(
        workspace,
        {
            "blocked_dir": b"original-blocked",
            "ok_file.txt": b"original-ok",
        },
    )

    # The first path is a directory, so writing its original bytes raises --
    # but the second path is still attempted and restored, and the failing
    # path comes back so the caller can report it (NF-2026-01469).
    assert failed == ["blocked_dir"]
    assert (workspace_path / "ok_file.txt").read_bytes() == b"original-ok"


def test_promote_failure_still_restores_the_retained_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _accept_env(tmp_path, monkeypatch)
    workspace_path = Path(
        manager.cards[TASK_ID]["terminal_review"]["evidence"]["workspace"]["path"]
    )

    def _raise_backwards_version(*_a, **_k):
        raise process_launcher.WorkspaceError("backwards_version")

    monkeypatch.setattr(manager, "_promote_accepted_candidate", _raise_backwards_version)

    # Promotion can raise before writing any byte (a backwards-version
    # refusal) or mid-write; either way the retained worktree must not be
    # left holding the CORRECTED bytes (NF-2026-01469).
    with pytest.raises(process_launcher.WorkspaceError):
        _accept(manager, manager_correction=_correction_payload())

    assert (workspace_path / TARGET).read_text(encoding="utf-8") == CANDIDATE
