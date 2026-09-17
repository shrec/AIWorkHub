"""``accept_review`` was moved out of ``process_launcher``; the seams must survive.

``ProcessManager.accept_review`` used to be 1183 lines inside
``process_launcher`` -- the single largest method in the repository's
highest-churn file.  It now lives in
:mod:`aiworkhub.process_launcher_accept_review` and the method is a delegation.

The move is only safe because the body still resolves its module-level
collaborators through the ``process_launcher`` module object.  27 test files
monkeypatch that module across 48 attributes; a relocation that captured those
names at import time instead would sever the seam **silently** -- the patch
would rebind ``process_launcher.X`` while the moved code kept calling the
original, and every one of those tests would still pass while testing nothing.

These tests are the guard.  They assert the binding list is exactly the moved
function's free-variable set (so a future edit that introduces a new global
cannot quietly escape the mechanism), and they drive two of the seams through
the real call path to prove a patch still lands.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import inspect
from pathlib import Path

import pytest

from aiworkhub import process_launcher
from aiworkhub import process_launcher_accept_review


SOURCE = Path(process_launcher_accept_review.__file__)


class _SeamReached(Exception):
    """Sentinel raised from a patched seam.

    Deliberately not a subclass of anything in the moved body's
    ``except (LaunchRejected, AttributeError, TypeError)`` pre-read guard, so
    reaching the seam is observable rather than swallowed.
    """


class _StubManager:
    """The moved function takes ``self`` as an ordinary parameter."""

    def _request_events(self, request_id: str) -> list[dict[str, object]]:
        return [{"task_id": "T-1", "request_id": request_id}]

    def _show_task(self, task_id: str) -> dict[str, object]:
        return {"task_id": task_id}


def _moved_function_node() -> ast.FunctionDef:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "accept_review"
    )


def _preamble_bindings(fn: ast.FunctionDef) -> dict[str, ast.Attribute]:
    """The ``name = _pl.name`` statements that re-bind the seams."""
    return {
        stmt.targets[0].id: stmt.value
        for stmt in fn.body
        if isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
        and isinstance(stmt.value, ast.Attribute)
        and isinstance(stmt.value.value, ast.Name)
        and stmt.value.value.id == "_pl"
        and stmt.value.attr == stmt.targets[0].id
    }


def _annotation_node_ids(fn: ast.FunctionDef) -> set[int]:
    """Every node reachable from an annotation.

    ``process_launcher_accept_review`` sets ``from __future__ import
    annotations``, so annotations are strings that are never evaluated.  A name
    appearing only there is not resolved at runtime and therefore cannot carry
    a monkeypatch -- it must NOT be re-bound, or it would be hidden from the
    type checker for no benefit.
    """
    ids: set[int] = set()
    for node in ast.walk(fn):
        annotation = getattr(node, "annotation", None)
        if annotation is not None:
            ids.update(id(child) for child in ast.walk(annotation))
    for arg in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs:
        if arg.annotation is not None:
            ids.update(id(child) for child in ast.walk(arg.annotation))
    if fn.returns is not None:
        ids.update(id(child) for child in ast.walk(fn.returns))
    return ids


def _free_names_of_moved_function() -> set[str]:
    """The names the body resolves AT RUNTIME without the seam preamble.

    The preamble binds every one of these as a local, so an ordinary
    free-variable pass over the current source returns nothing.  Ignoring the
    preamble's own assignments recovers the set the body actually depends on --
    which is precisely the set that has to be re-bound.  Annotation-only names
    are excluded because they are never evaluated.
    """
    fn = _moved_function_node()
    preamble = _preamble_bindings(fn)
    annotation_ids = _annotation_node_ids(fn)

    bound = {arg.arg for arg in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs}
    if fn.args.vararg:
        bound.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        bound.add(fn.args.kwarg.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if node.id not in preamble:
                bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node is not fn:
                bound.add(node.name)

    used = {
        node.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and id(node) not in annotation_ids
    }
    return {name for name in used - bound if not hasattr(builtins, name)}


def test_declared_seam_names_are_exactly_the_functions_free_variables():
    """A new global in the body must be declared, or this fails.

    The preamble is what keeps the seams connected.  A runtime free name that is
    not in it would raise ``NameError``; a name in the preamble that the body no
    longer uses at runtime is dead weight that also hides the name from the type
    checker.  Both are drift, and both are caught here.

    A helper defined in THIS module is the second, disjoint group: it has no
    seam in ``process_launcher`` to preserve, so it resolves as an ordinary
    global and is declared in ``ACCEPT_REVIEW_LOCAL_NAMES``.  The union is still
    exact, so an undeclared global is still caught.
    """
    declared = set(process_launcher_accept_review.ACCEPT_REVIEW_SEAM_NAMES) | set(
        process_launcher_accept_review.ACCEPT_REVIEW_LOCAL_NAMES
    )
    assert declared == _free_names_of_moved_function()


def test_local_names_live_here_and_are_not_process_launcher_seams():
    """The two groups must stay disjoint, or the seam guard means nothing.

    A name in ``ACCEPT_REVIEW_LOCAL_NAMES`` that also exists on
    ``process_launcher`` would be patchable in two places and read from one --
    the silent-severance failure this whole module exists to prevent, wearing a
    different hat.
    """
    local = set(process_launcher_accept_review.ACCEPT_REVIEW_LOCAL_NAMES)
    assert local and not local & set(
        process_launcher_accept_review.ACCEPT_REVIEW_SEAM_NAMES
    )
    assert [
        name for name in sorted(local) if not hasattr(process_launcher_accept_review, name)
    ] == []
    assert [name for name in sorted(local) if hasattr(process_launcher, name)] == []


def test_every_declared_seam_is_still_an_attribute_of_process_launcher():
    """Each name must resolve on the module the tests actually patch."""
    missing = [
        name
        for name in process_launcher_accept_review.ACCEPT_REVIEW_SEAM_NAMES
        if not hasattr(process_launcher, name)
    ]
    assert missing == []


def test_seams_are_read_from_process_launcher_at_call_time_not_import_time():
    """The preamble must read the live module, never a captured import.

    An import-time capture is exactly the silent failure this move risks, and
    it is visible in the source: every binding is an attribute load off the
    ``process_launcher`` module imported inside the function body.
    """
    fn = _moved_function_node()

    # The module is imported inside the function, so the binding below reads the
    # live module object rather than a name captured when this module loaded.
    assert any(
        isinstance(stmt, ast.ImportFrom)
        and any(a.name == "process_launcher" and a.asname == "_pl" for a in stmt.names)
        for stmt in fn.body
    )

    # _preamble_bindings only matches `X = _pl.X`, so covering every declared
    # seam is itself the proof that each one is a live attribute read.
    bindings = _preamble_bindings(fn)
    missing = [
        name
        for name in process_launcher_accept_review.ACCEPT_REVIEW_SEAM_NAMES
        if name not in bindings
    ]
    assert missing == []


def test_delegating_method_keeps_the_original_signature():
    """Callers and the 16 test files that reference the name see no change."""
    assert str(inspect.signature(process_launcher.ProcessManager.accept_review)) == str(
        inspect.signature(process_launcher_accept_review.accept_review)
    )


def test_process_launcher_method_is_a_delegation_and_holds_no_logic():
    """The point of the move: the body is gone from process_launcher."""
    source = inspect.getsource(process_launcher.ProcessManager.accept_review)
    assert len(source.splitlines()) < 40
    assert "_accept_review_impl(" in source


@pytest.mark.parametrize("seam", ["_parse_card", "_canonical_task_status"])
def test_patching_process_launcher_still_reaches_the_moved_body(monkeypatch, seam):
    """Drive two real seams through the real call path.

    ``_parse_card`` and ``_canonical_task_status`` are both consumed by the
    pre-read at the top of the moved body.  Patching them on
    ``process_launcher`` must change what the moved code executes; if the
    relocation had captured either name at import time the sentinel would never
    be raised and this test would fail.
    """
    if seam == "_canonical_task_status":
        # _parse_card runs first; let it succeed so the later seam is reached.
        monkeypatch.setattr(process_launcher, "_parse_card", lambda *a, **k: {})

    def _sentinel(*args, **kwargs):
        raise _SeamReached(seam)

    monkeypatch.setattr(process_launcher, seam, _sentinel)

    with pytest.raises(_SeamReached) as excinfo:
        process_launcher.ProcessManager.accept_review(
            _StubManager(), "R-1", "T-1"
        )
    assert str(excinfo.value) == seam


def test_accept_review_uses_terminal_identity_before_later_orchestrator_event():
    """Bookkeeping appended after review_ready cannot hide execution identity."""

    class _Manager:
        repo = Path("/nonexistent")

        def _request_events(self, request_id: str) -> list[dict[str, object]]:
            return [
                {
                    "request_id": request_id,
                    "task_id": "T-1",
                    "runner": "codex_gpt-5.5",
                    "topic": "code",
                    "adapter_id": "codex_cli",
                    "state": "review_ready",
                },
                {
                    "request_id": request_id,
                    "task_id": "T-1",
                    "event_type": "review_orchestrator_wait",
                },
            ]

        def _show_task(self, task_id: str) -> dict[str, object]:
            raise process_launcher.LaunchRejected("stop-after-identity")

        def _promotion_lock(self):
            return contextlib.nullcontext()

    result = process_launcher_accept_review.accept_review(_Manager(), "R-1", "T-1")

    assert result["error"] == "task_lookup_failed:stop-after-identity"


def test_latest_request_identity_event_never_borrows_another_tasks_identity():
    events = [
        {"task_id": "T-OTHER", "runner": "worker", "topic": "code"},
        {"task_id": "T-1", "event_type": "review_orchestrator_wait"},
    ]

    assert (
        process_launcher_accept_review._latest_request_identity_event(events, "T-1")
        is None
    )


# ---------------------------------------------------------------------------
# The accept parameter surface (audit 2026-09-08, problem 4).
#
# Measured over 159 accept attempts: the manager retyped ``reviewer_request_ids``
# in 135 of them, and 49 (31%) failed on a parameter or timing condition --
# required_reviewer_missing 19, quality_reviewer_not_review_ready 17,
# terminal_substatus_not_review_ready 8, explicit_human_approval_missing 5 --
# every one of them AFTER a combined-tree materialization and two validation
# runs.
# ---------------------------------------------------------------------------

import json
import sqlite3

from aiworkhub import quality_evidence, task_store


def _reviewer_card(
    task_id: str,
    lens: str,
    *,
    target_task_id: str = "T-1",
    target_request_id: str = "R-1",
    findings: list[dict] | None = None,
    substatus: str = "review_ready",
) -> dict:
    return {
        "task_id": task_id,
        "topic": "quality_review",
        "terminal_review": {
            "substatus": substatus,
            "evidence": {
                "quality_review": {
                    "lens": lens,
                    "packet_sha256": "d" * 64,
                    "target_task_id": target_task_id,
                    "target_request_id": target_request_id,
                },
                "quality_review_receipt": {
                    "packet_sha256": "d" * 64,
                    "report": {"lens": lens, "findings": list(findings or [])},
                },
            },
        },
    }


def _store_with_reviewers(tmp_path: Path, cards: list[dict]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        for card in cards:
            conn.execute(
                "INSERT INTO tasks (task_id, runner, topic, mode, status, "
                "worker_status, priority, objective, card_json, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    card["task_id"], "reviewer", "quality_review", "auto", "review",
                    "done", 5, "review", json.dumps(card),
                    "2026-09-08T00:00:00+00:00", "2026-09-08T00:00:00+00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return repo


class _ReviewerManager:
    """The three collaborators the reviewer-resolution path actually uses."""

    def __init__(self, repo: Path, events: dict[str, dict] | None = None) -> None:
        self.repo = repo
        self._events = events or {}

    def _latest_by_request(self) -> dict[str, dict]:
        return self._events

    def _context_write_intent_snapshot(self, request_id: str) -> dict:
        return {"ok": True, "counts": {"pending": 0}, "intents": []}


def test_bound_reviewer_rows_reads_the_binding_the_finalizer_writes(tmp_path: Path):
    """Measured on the live store: 2,219 of 2,219 reviewer cards carry the
    binding under ``terminal_review.evidence.quality_review`` and NONE under the
    root ``quality_review`` key, which is why a root-only read finds nothing."""
    repo = _store_with_reviewers(
        tmp_path,
        [
            _reviewer_card("QR-CORRECTNESS", "correctness"),
            _reviewer_card("QR-SECURITY", "security"),
            _reviewer_card("QR-OTHER", "correctness", target_request_id="R-OTHER"),
        ],
    )
    rows = process_launcher_accept_review.bound_reviewer_rows(repo, "T-1", "R-1")

    assert [row["task_id"] for row in rows] == ["QR-CORRECTNESS", "QR-SECURITY"]
    assert [row["lens"] for row in rows] == ["correctness", "security"]
    assert process_launcher_accept_review.bound_reviewer_task_ids(
        repo, "T-1", "R-1"
    ) == ["QR-CORRECTNESS", "QR-SECURITY"]


def test_bound_reviewer_rows_uses_an_ungated_readonly_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Reviewer enumeration must not join the single-writer queue."""
    repo = _store_with_reviewers(
        tmp_path,
        [_reviewer_card("QR-CORRECTNESS", "correctness")],
    )
    observed: list[bool] = []
    real_connect = task_store._connect

    def traced_connect(path, **kwargs):  # type: ignore[no-untyped-def]
        observed.append(bool(kwargs.get("readonly")))
        return real_connect(path, **kwargs)

    monkeypatch.setattr(task_store, "_connect", traced_connect)

    rows = process_launcher_accept_review.bound_reviewer_rows(repo, "T-1", "R-1")

    assert [row["task_id"] for row in rows] == ["QR-CORRECTNESS"]
    assert observed
    assert all(observed)


def test_reviewer_evidence_separates_running_from_missing(tmp_path: Path):
    """"Not finished" and "not launched" need different answers from a manager;
    the accept surface returned the same one for both."""
    repo = _store_with_reviewers(
        tmp_path,
        [
            _reviewer_card("QR-CORRECTNESS", "correctness"),
            _reviewer_card("QR-SECURITY", "security"),
        ],
    )
    manager = _ReviewerManager(
        repo,
        {
            "rq-correctness-old": {
                "task_id": "QR-CORRECTNESS", "state": "review_ready",
                "finished_at": "2026-09-08T01:00:00+00:00",
            },
            "rq-correctness": {
                "task_id": "QR-CORRECTNESS", "state": "review_ready",
                "finished_at": "2026-09-08T02:00:00+00:00",
            },
            "rq-security": {"task_id": "QR-SECURITY", "state": "running"},
        },
    )
    rows = process_launcher_accept_review.reviewer_evidence(manager, "T-1", "R-1")

    assert [(row["lens"], row["request_id"], row["usable"]) for row in rows] == [
        ("correctness", "rq-correctness", True),
        ("security", "rq-security", False),
    ]
    # The default the manager no longer has to type.
    assert process_launcher_accept_review.bound_reviewer_request_ids(
        manager, "T-1", "R-1"
    ) == ["rq-correctness"]


def _profile(tier: str) -> dict:
    return quality_evidence.resolve_risk_profile(tier)


def test_fold_defaults_to_the_server_bound_reviewers_and_names_what_is_left():
    reviewers = [
        {"task_id": "QR-C", "lens": "correctness", "request_id": "rq-c",
         "state": "review_ready", "usable": True, "receipt": None},
        {"task_id": "QR-S", "lens": "security", "request_id": "rq-s",
         "state": "running", "usable": False, "receipt": None},
    ]
    fold = process_launcher_accept_review.fold_accept_blockers(
        reviewers=reviewers,
        reviewer_request_ids=None,
        risk_profile=_profile("high"),
        terminal_substatus="review_ready",
        confirm_high_risk=True,
    )

    assert fold["reviewer_request_ids"] == ["rq-c"]
    assert fold["reviewer_request_id_source"] == "server_bound_reviewer_children"
    assert [row["error"] for row in fold["blockers"]] == [
        "required_reviewer_missing:security"
    ]
    # "wait" and "launch" are different next actions and the fold says which.
    assert fold["blockers"][0]["reviewer_running"] is True


def test_fold_never_invents_a_refusal_for_a_reviewer_it_cannot_see():
    """A manager-named id outside the bound-children scan is not evidence of
    absence: the authoritative loop resolves it by request id and verifies its
    receipt, so the lens census here is simply incomplete and says so."""
    fold = process_launcher_accept_review.fold_accept_blockers(
        reviewers=[],
        reviewer_request_ids=["rq-named-by-hand"],
        risk_profile=_profile("medium"),
        terminal_substatus="review_ready",
    )

    assert fold["blockers"] == []
    assert fold["lens_census_complete"] is False
    assert fold["unresolved_reviewer_request_ids"] == ["rq-named-by-hand"]
    assert fold["reviewer_request_ids"] == ["rq-named-by-hand"]


def test_fold_reports_the_cheap_blockers_in_decision_order():
    reviewers = [
        {"task_id": "QR-C", "lens": "correctness", "request_id": "rq-c",
         "state": "running", "usable": False, "receipt": None},
    ]
    fold = process_launcher_accept_review.fold_accept_blockers(
        reviewers=reviewers,
        reviewer_request_ids=["rq-c"],
        risk_profile=_profile("critical"),
        terminal_substatus="validation_failed",
        pending_context_write_intents=2,
        confirm_high_risk=False,
        destructive_blockers=["destructive-diff"],
    )

    assert [row["kind"] for row in fold["blockers"]] == [
        "terminal_substatus_not_review_ready",
        "context_write_intents_pending",
        "quality_reviewer_not_review_ready",
        "required_reviewer_missing",
        "required_reviewer_missing",
        "required_reviewer_missing",
        "explicit_human_approval_missing",
        "destructive_diff_requires_manager_confirmation",
    ]
    assert set(process_launcher_accept_review.ACCEPT_BLOCKER_KINDS) >= {
        row["kind"] for row in fold["blockers"]
    }


def test_fold_names_refinement_from_reports_already_verified():
    """A correctness defect refuses acceptance whatever its severity; the fold
    reads it off the sealed receipt instead of re-running the reviewer."""
    reviewers = [
        {
            "task_id": "QR-C", "lens": "correctness", "request_id": "rq-c",
            "state": "review_ready", "usable": True,
            "receipt": {
                "report": {
                    "lens": "correctness",
                    "findings": [
                        {"id": "F-1", "severity": "low", "disposition": "defect"},
                        {"id": "F-2", "severity": "low", "disposition": "observation"},
                    ],
                }
            },
        },
    ]
    fold = process_launcher_accept_review.fold_accept_blockers(
        reviewers=reviewers,
        reviewer_request_ids=None,
        risk_profile=_profile("medium"),
        terminal_substatus="review_ready",
    )

    assert [row["error"] for row in fold["blockers"]] == [
        "refinement_required:reviewer:correctness:F-1"
    ]


@pytest.mark.parametrize(
    ("declared", "requested", "expected"),
    [
        ("", None, "low"),
        ("high", None, "high"),
        ("high", "low", "high"),
        ("high", "critical", "critical"),
        ("", "medium", "medium"),
    ],
)
def test_requested_tier_overrides_the_server_tier_upward_only(
    declared: str, requested, expected: str
):
    """An override of a safety floor may raise it and may never lower it."""
    card = {
        "terminal_review": {
            "evidence": {
                "quality_gate": {
                    "review_risk_profile": {"effective_tier": declared, "error": ""}
                }
            }
        }
    }
    assert process_launcher_accept_review.effective_requested_risk_tier(
        card, requested
    ) == expected


def test_accept_preview_is_read_only_and_answers_before_any_materialization(
    monkeypatch, tmp_path: Path
):
    """The whole point: the answer arrives without a workspace, a combined tree
    or a validation run -- the three things 49 of 159 failed attempts paid for
    before being told a parameter was wrong."""
    repo = _store_with_reviewers(tmp_path, [_reviewer_card("QR-C", "correctness")])
    manager = _ReviewerManager(
        repo, {"rq-c": {"task_id": "QR-C", "state": "running"}}
    )
    card = {
        "task_id": "T-1",
        "risk_tier": "high",
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {"changed_paths": ["src/aiworkhub/process_launcher.py"]},
        },
    }
    manager._show_task = lambda task_id: {"returncode": 0, "stdout": json.dumps(card)}
    for name in ("create_combined_validation_workspace", "promote", "enforce_scope"):
        monkeypatch.setattr(
            process_launcher, name,
            lambda *a, **k: (_ for _ in ()).throw(AssertionError(name + "_must_not_run")),
        )

    preview = process_launcher_accept_review.accept_preview(manager, "R-1", "T-1")

    assert preview["ok"] is True and preview["evaluated"] is True
    assert preview["authoritative"] is False
    assert preview["blocked"] is True
    assert preview["risk_profile"]["effective_tier"] == "high"
    assert preview["risk_profile"]["required_reviewer_lenses"] == [
        "correctness", "security"
    ]
    assert [row["error"] for row in preview["blockers"]] == [
        "required_reviewer_missing:correctness",
        "required_reviewer_missing:security",
        "explicit_human_approval_missing",
    ]
    # One blocker per required lens, each carrying the manager's next action:
    # correctness is already running (wait), security is not (launch).
    assert [row["reviewer_running"] for row in preview["blockers"][:2]] == [True, False]
    assert preview["per_lens"][0]["lens"] == "correctness"
    assert preview["per_lens"][0]["state"] == "running"


def test_accept_preview_never_raises_on_an_unreadable_card():
    class _Broken:
        repo = Path("/nonexistent")

        def _show_task(self, task_id: str) -> dict:
            raise process_launcher.LaunchRejected("gone")

    result = process_launcher_accept_review.accept_preview(_Broken(), "R-1", "T-1")
    assert result["ok"] is False and result["evaluated"] is False
    assert result["error"].startswith("task_lookup_failed:")


# ---------------------------------------------------------------------------
# NF-805 / NF-2026-00884: automatic supplemental inspection, on the real path.
#
# The regression: a 23-hunk candidate whose correctness reviewer came back with
# nothing but ``process_limit`` findings. The gate said
# ``reviewer_could_not_inspect`` -- correctly -- and then nothing happened.
# Whoever read the blocker had two bad options: relaunch the implementation
# worker, whose bytes were never in question, or hand-launch a reviewer.
#
# The tests below drive ``accept_review`` itself, through its declared seams,
# so the transition being proven is the production one. The fold is never
# called twice by hand here: the second round exists because the first accept
# created it.
# ---------------------------------------------------------------------------

import hashlib
from types import SimpleNamespace

from aiworkhub import evidence_instruments, learning_commit_store, needfix_store
from aiworkhub import quality_reviewer

_SUPPLEMENTAL_CHANGED = ("src/alpha.py", "src/beta.py")


def _supplemental_segments(count: int, *, truncated: bool) -> list[dict]:
    return [
        {
            "kind": "replace",
            "candidate_start_line": 10 * index + 1,
            "candidate_end_line": 10 * index + 4,
            "changed_start_line": 10 * index + 1,
            "changed_end_line": 10 * index + 4,
            "baseline_start_line": 10 * index + 1,
            "baseline_end_line": 10 * index + 4,
            "excerpt_bytes": 40,
            "truncated": truncated,
        }
        for index in range(count)
    ]


class _StubWorkspace:
    def __init__(self, *, repo: Path, path: Path, home: Path, request_id: str) -> None:
        self.repo = repo
        self.path = path
        self.home = home
        self.request_id = request_id

    def as_metadata(self) -> dict:
        return {"request_id": self.request_id, "path": str(self.path)}


class _WorkspaceRegistry:
    """Stands in for ``WorkerWorkspace``; resolves metadata by request id."""

    def __init__(self) -> None:
        self.by_request: dict[str, _StubWorkspace] = {}

    def add(self, workspace: _StubWorkspace) -> _StubWorkspace:
        self.by_request[workspace.request_id] = workspace
        return workspace

    def from_metadata(self, metadata: dict) -> _StubWorkspace:
        return self.by_request[str(metadata["request_id"])]


class _EvidenceLevelsStub:
    class EvidenceValidationError(Exception):
        pass

    class EvidenceLevel:
        FIXED_AND_VERIFIED = "fixed_and_verified"

    @staticmethod
    def validate_evidence_record(record):
        return SimpleNamespace(
            evidence_level="fixed_and_verified",
            reference="attempt:R-1",
            to_dict=lambda: {"reference": "attempt:R-1"},
        )

    @staticmethod
    def meets_evidence_level(observed, required) -> bool:
        return True


class _QualityEvidenceProxy:
    """The real ``quality_evidence``, minus the two collaborators needing git.

    The gate under test -- ``run_completion_quality_gate`` and the fold beneath
    it -- is the REAL one. Only the destructive-diff and policy-authority
    probes, which compare a canonical tree against a candidate one, are
    answered with their no-finding results.
    """

    def __getattr__(self, name):
        return getattr(quality_evidence, name)

    @staticmethod
    def run_destructive_diff_checks(repo, workspace_path, *, changed_paths):
        return []

    @staticmethod
    def assess_quality_policy_authority(repo, workspace_path, *, changed_paths):
        return {
            "weakened": False,
            "escalation_signal": "",
            "candidate_policy_source": "candidate_declared",
            "canonical_declared_checks": 0,
            "canonical_config_readable": True,
        }


_BLIND_FINDINGS = [
    {
        "id": "PL-1",
        "severity": "low",
        "disposition": "process_limit",
        "summary": "the reviewer could not read its packet",
        "evidence": "no packet bytes reached the reviewer process",
    }
]


class _AcceptManager:
    """Only the collaborators ``accept_review`` actually calls on ``self``."""

    def __init__(self, repo: Path, process_dir: Path) -> None:
        self.repo = repo
        self.process_dir = process_dir
        self.events: dict[str, list[dict]] = {}
        self.latest: dict[str, dict] = {}
        self.cards: dict[str, dict] = {}
        self.reviewer_launches: list[dict] = []
        self.implementation_relaunches = 0

    # -- lifecycle seams ----------------------------------------------------
    def _promotion_lock(self):
        return contextlib.nullcontext()

    def _request_lock(self, request_id: str):
        return contextlib.nullcontext()

    def _request_events(self, request_id: str) -> list[dict]:
        return [dict(row) for row in self.events.get(request_id, [])]

    def _show_task(self, task_id: str) -> dict:
        return self.cards[task_id]

    def _latest_by_request(self) -> dict:
        return {key: dict(value) for key, value in self.latest.items()}

    def _metadata_from_events(self, events: list[dict]) -> Path:
        return self.process_dir / f"{events[-1]['request_id']}.json"

    # -- evidence seams -----------------------------------------------------
    def _context_write_intent_snapshot(self, request_id: str) -> dict:
        return {"ok": True, "counts": {"pending": 0}, "intents": []}

    def _verify_attempt_artifact_receipt(self, request_id, manifest) -> dict:
        return {"schema_id": "aiworkhub.attempt_artifact_manifest.v1"}

    def _minimum_acceptance_evidence_level(self, card, **kwargs) -> str:
        return "fixed_and_verified"

    def _attempt_evidence_reference(self, request_id, receipt) -> str:
        return "attempt:R-1"

    def _canonical_outcome_evidence(self, request_id, receipt, **kwargs) -> dict:
        return {"reference": "attempt:R-1"}

    def _candidate_reachability_inputs(self, workspace, changed):
        return None

    # -- promotion seams ----------------------------------------------------
    def _promote_accepted_candidate(self, workspace, changed) -> list[str]:
        return list(changed)

    def _close_accepted_task_needfix(self, task_id, request_id) -> dict:
        return {}

    def _retention_event(self, payload, disposition="") -> None:
        return None

    # -- the two launches this card is about --------------------------------
    def launch_quality_reviewer(self, **kwargs) -> dict:
        self.reviewer_launches.append(dict(kwargs))
        return {
            "ok": True,
            "request_id": "rq-" + str(kwargs["reviewer_task_id"]).lower(),
            "task_id": kwargs["reviewer_task_id"],
        }

    def _launch_isolated(self, *args, **kwargs):
        self.implementation_relaunches += 1
        raise AssertionError("the implementation worker must never be relaunched")


def _supplemental_env(tmp_path: Path, monkeypatch, reviewer_specs: list[dict]):
    """One ``review_ready`` 23-hunk candidate plus its bound reviewer children.

    ``reviewer_specs`` rows carry ``task_id``/``request_id``/``lens``, the
    reviewer's ``findings``, and ``packet``: ``"sealed"`` writes the real
    packet, ``"tampered"`` writes one whose body no longer hashes to the digest
    its receipt is bound to, and ``"absent"`` writes none at all.
    """

    repo = _store_with_reviewers(
        tmp_path,
        [
            _reviewer_card(
                spec["task_id"], spec["lens"], findings=list(spec["findings"])
            )
            for spec in reviewer_specs
        ],
    )
    process_dir = tmp_path / "processes"
    candidate = tmp_path / "candidate"
    (candidate / "src").mkdir(parents=True)
    process_dir.mkdir()
    for index, relative in enumerate(_SUPPLEMENTAL_CHANGED):
        (candidate / relative).write_text(
            f"def alpha_{index}():\n    return {index}\n", encoding="utf-8"
        )
    stored_hashes = {
        relative: hashlib.sha256((candidate / relative).read_bytes()).hexdigest()
        for relative in _SUPPLEMENTAL_CHANGED
    }
    # 11 hunks carried inline, 12 reachable only through the packet-bound
    # candidate overlay: 23, the exact NF-805 shape.
    packet = quality_reviewer.build_review_packet(
        request_id="R-1",
        task_id="T-1",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes=stored_hashes,
        source_evidence={
            "src/alpha.py": {
                "candidate_sha256": stored_hashes["src/alpha.py"],
                "excerpt": "@@ alpha @@\n+return 0\n",
                "excerpt_bytes": 22,
                "source_bytes": 22,
                "truncated": False,
                "diff_complete": True,
                "segments": _supplemental_segments(11, truncated=False),
            },
            "src/beta.py": {
                "candidate_sha256": stored_hashes["src/beta.py"],
                "excerpt": "@@ beta @@\n+return 1\n",
                "excerpt_bytes": 21,
                "source_bytes": 8192,
                "truncated": True,
                "segments": _supplemental_segments(12, truncated=True),
                "omission_reason": "changed_hunks_omitted:12",
            },
        },
    )

    registry = _WorkspaceRegistry()
    manager = _AcceptManager(repo, process_dir)
    target_workspace = registry.add(
        _StubWorkspace(
            repo=repo, path=candidate, home=tmp_path / "target-home", request_id="R-1"
        )
    )
    manager.events["R-1"] = [
        {
            "request_id": "R-1", "task_id": "T-1", "runner": "codex_gpt-5.5",
            "topic": "code", "adapter_id": "codex_cli", "state": "review_ready",
        }
    ]
    manager.cards["T-1"] = {
        "task_id": "T-1",
        "runner": "codex_gpt-5.5",
        "topic": "code",
        "claimed_by": "codex_gpt-5.5",
        "claim_epoch": 1,
        "required_outputs": [],
        "validation": ["true"],
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {
                    "request_id": "R-1", "task_id": "T-1",
                    "runner": "codex_gpt-5.5", "topic": "code",
                },
                "changed_paths": list(_SUPPLEMENTAL_CHANGED),
                "changed_path_hashes": dict(stored_hashes),
                "workspace": {"request_id": "R-1"},
                "evidence_record": {"reference": "attempt:R-1"},
                "validation": [],
            },
        },
    }

    receipts: dict[str, dict] = {}
    for spec in reviewer_specs:
        request_id = spec["request_id"]
        home = tmp_path / f"home-{request_id}"
        home.mkdir()
        registry.add(
            _StubWorkspace(
                repo=repo, path=home / "tree", home=home, request_id=request_id
            )
        )
        packet_path = home / "quality_review_packet.json"
        if spec["packet"] == "sealed":
            packet_path.write_text(json.dumps(packet), encoding="utf-8")
        elif spec["packet"] == "tampered":
            forged = json.loads(json.dumps(packet))
            forged["contract"]["objective"] = "rewritten after sealing"
            packet_path.write_text(json.dumps(forged), encoding="utf-8")
        (process_dir / f"{request_id}.json").write_text(
            json.dumps(
                {
                    "task_id": spec["task_id"],
                    "request_id": request_id,
                    "adapter_id": "claude_cli",
                    "workspace": {"request_id": request_id},
                    "quality_review": {
                        "lens": spec["lens"],
                        "packet_path": str(packet_path),
                        "target_request_id": "R-1",
                        "target_task_id": "T-1",
                    },
                }
            ),
            encoding="utf-8",
        )
        manager.events[request_id] = [
            {
                "request_id": request_id, "task_id": spec["task_id"],
                "runner": "claude_sonnet5", "topic": "quality_review",
                "adapter_id": "claude_cli", "state": "review_ready",
            }
        ]
        manager.latest[request_id] = {
            "task_id": spec["task_id"], "state": "review_ready",
            "finished_at": "2026-09-16T00:00:0%d+00:00" % len(receipts),
        }
        receipts[request_id] = {
            "packet_sha256": packet["packet_sha256"],
            "report": {
                "lens": spec["lens"],
                "provider": "claude_cli",
                "read_only": True,
                "can_mutate_repo": False,
                "findings": list(spec["findings"]),
            },
        }

    seams = {
        "_parse_card": lambda raw, task_id: raw,
        "_canonical_task_status": lambda card: "review",
        "_finished_acceptance_result": lambda *a, **k: None,
        "_card_is_readonly_quality_review": lambda card: False,
        "_card_is_readonly_research": lambda card: False,
        "WorkerWorkspace": registry,
        "assert_gc_safe_workspace_shape": lambda *a, **k: None,
        "evidence_levels": _EvidenceLevelsStub,
        "core": SimpleNamespace(
            writes_allowed=lambda: True,
            CODEX_RUNNER="codex",
            _claude_manager_identity=lambda: {},
            _codex_manager_identity=lambda: {},
        ),
        "_worker_workspace": SimpleNamespace(
            finalization_git_timeout_seconds=lambda: 30
        ),
        "enforce_scope": lambda workspace, **k: (
            list(_SUPPLEMENTAL_CHANGED) if workspace is target_workspace else []
        ),
        "validate_required_outputs": lambda *a, **k: [],
        "quality_evidence": _QualityEvidenceProxy(),
        "create_combined_validation_workspace": lambda workspace, card, changed: (
            target_workspace, {"schema_id": "aiworkhub.combined_tree.v1"}
        ),
        "_run_declared_validations": lambda *a, **k: [],
        "cleanup_workspace": lambda *a, **k: None,
        "_changed_path_hashes": lambda workspace, changed: dict(stored_hashes),
        "_verified_quality_review_receipt": (
            lambda metadata, workspace, request_id: receipts[request_id]
        ),
        "_run_full_snapshot_validations": lambda *a, **k: ([], {}),
        "_enforce_behavioral_gate": lambda *a, **k: None,
        "_accepted_outcome_receipt": lambda *a, **k: {"schema_id": "accepted"},
        "task_engine": SimpleNamespace(
            accept_review=lambda *a, **k: {"ok": True},
            disposition_reviewer_children=lambda *a, **k: {
                "ok": True, "stdout": "{}"
            },
        ),
        "learning_commit": SimpleNamespace(commit_owed=lambda **k: {}),
    }
    for name, value in seams.items():
        monkeypatch.setattr(process_launcher, name, value)
    monkeypatch.setattr(
        evidence_instruments, "review_evidence_audit",
        lambda *a, **k: {"blocking": False, "blockers": []},
    )
    monkeypatch.setattr(
        process_launcher_accept_review, "manager_skill_tools",
        SimpleNamespace(record_decision_evidence=lambda *a, **k: None),
    )
    monkeypatch.setattr(
        needfix_store, "draft_from_review_evidence", lambda *a, **k: []
    )
    monkeypatch.setattr(
        learning_commit_store, "record_decision_event", lambda *a, **k: {}
    )
    return manager, packet


def _accept(manager):
    return process_launcher_accept_review.accept_review(
        manager, "R-1", "T-1", requested_risk_tier="medium"
    )


def test_supplemental_rounds_completed_counts_only_the_extra_same_lens_reviewers():
    """The first reviewer for a lens IS the review; every later one is a round."""
    rows = [
        {"lens": "correctness", "task_id": "QR-C"},
        {"lens": "correctness", "task_id": "QR-C-SUPPLEMENTAL-1"},
        {"lens": "security", "task_id": "QR-S"},
        {"lens": "", "task_id": "QR-UNBOUND"},
    ]

    assert process_launcher_accept_review.supplemental_rounds_completed(rows) == {
        "correctness": 1, "security": 0,
    }


def test_sealed_supplemental_packet_is_refused_outside_the_reviewer_home(
    tmp_path: Path,
):
    """A packet path the reviewer's own home does not contain is not evidence."""
    home = tmp_path / "home"
    elsewhere = tmp_path / "elsewhere"
    home.mkdir()
    elsewhere.mkdir()
    packet = {"candidate": {}}
    packet["packet_sha256"] = quality_reviewer._canonical_digest(packet)
    outside = elsewhere / "packet.json"
    outside.write_text(json.dumps(packet), encoding="utf-8")
    workspace = SimpleNamespace(home=home)
    metadata = {"quality_review": {"packet_path": str(outside)}}

    assert process_launcher_accept_review.sealed_review_packet(
        metadata, workspace, {"packet_sha256": packet["packet_sha256"]}
    ) is None


def test_process_limit_review_automatically_creates_one_supplemental_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The whole card, on the production path.

    The accept still fails, with the error it always produced -- but by the
    time it returns, the bounded same-lens re-read of the SAME sealed packet
    has already been launched. Nothing asked a manager to do it, and the
    implementation worker was never touched.
    """
    manager, packet = _supplemental_env(
        tmp_path, monkeypatch,
        [{"task_id": "QR-C", "request_id": "rq-c", "lens": "correctness",
          "findings": _BLIND_FINDINGS, "packet": "sealed"}],
    )

    result = _accept(manager)

    assert result["ok"] is False
    assert "reviewer_could_not_inspect:correctness" in result["error"]
    assert manager.implementation_relaunches == 0
    assert len(manager.reviewer_launches) == 1
    launch = manager.reviewer_launches[0]
    assert launch["lens"] == "correctness"
    assert launch["target_request_id"] == "R-1"
    assert launch["target_task_id"] == "T-1"
    assert launch["reviewer_task_id"] == "QR-C-SUPPLEMENTAL-1"
    # The same route the blind reviewer itself ran on: a re-read, not a reroute.
    assert (launch["runner"], launch["adapter_id"]) == ("claude_sonnet5", "claude_cli")
    row = result["supplemental_inspection"][0]
    assert row["created"] is True and row["eligible"] is True
    assert row["round"] == 1 and row["max_rounds"] == 1
    assert row["packet_sha256"] == packet["packet_sha256"]
    assert row["inspection_target_count"] == len(_SUPPLEMENTAL_CHANGED)
    assert row["implementation_worker_relaunched"] is False


def test_supplemental_round_is_refused_when_the_packet_cannot_be_proven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Missing evidence stays missing.

    A packet whose body no longer hashes to the digest its receipt is bound to
    is not this reviewer's packet, so no round is created -- and the lens stays
    exactly as blocked as it was.
    """
    manager, _packet = _supplemental_env(
        tmp_path, monkeypatch,
        [{"task_id": "QR-C", "request_id": "rq-c", "lens": "correctness",
          "findings": _BLIND_FINDINGS, "packet": "tampered"}],
    )

    result = _accept(manager)

    assert result["ok"] is False
    assert "reviewer_could_not_inspect:correctness" in result["error"]
    assert manager.reviewer_launches == []
    row = result["supplemental_inspection"][0]
    assert row["eligible"] is False and row["created"] is False
    assert row["reason"] == "packet_evidence_missing"


def test_repeated_blindness_stops_at_the_bounded_supplemental_attempt_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The round this card creates is the LAST one.

    Two bound correctness reviewers means one supplemental round already
    happened; the second blindness is not a transient, so the accept stays
    blocked and nothing new is launched.
    """
    manager, _packet = _supplemental_env(
        tmp_path, monkeypatch,
        [
            {"task_id": "QR-C", "request_id": "rq-c", "lens": "correctness",
             "findings": _BLIND_FINDINGS, "packet": "sealed"},
            {"task_id": "QR-C-SUPPLEMENTAL-1", "request_id": "rq-c-s1",
             "lens": "correctness", "findings": _BLIND_FINDINGS,
             "packet": "sealed"},
        ],
    )

    result = _accept(manager)

    assert result["ok"] is False
    assert "reviewer_could_not_inspect:correctness" in result["error"]
    assert manager.reviewer_launches == []
    row = result["supplemental_inspection"][0]
    assert row["eligible"] is False and row["reason"] == "attempt_limit_reached"
    assert row["round"] == 2


def test_the_supplemental_rounds_sighted_report_makes_the_candidate_acceptable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The second production transition, on the round the first one created.

    Same 23-hunk candidate, same sealed packet, same lens -- and this time the
    reviewer read it. Nothing about the candidate changed between the two
    transitions, which is the point: the first failure was the review process,
    not the code.
    """
    manager, _packet = _supplemental_env(
        tmp_path, monkeypatch,
        [
            {"task_id": "QR-C", "request_id": "rq-c", "lens": "correctness",
             "findings": _BLIND_FINDINGS, "packet": "sealed"},
            {"task_id": "QR-C-SUPPLEMENTAL-1", "request_id": "rq-c-s1",
             "lens": "correctness", "findings": [], "packet": "sealed"},
        ],
    )

    result = _accept(manager)

    assert result["ok"] is True, result
    assert result["promoted_paths"] == list(_SUPPLEMENTAL_CHANGED)
    assert "supplemental_inspection" not in result
    assert manager.reviewer_launches == []
    assert manager.implementation_relaunches == 0
