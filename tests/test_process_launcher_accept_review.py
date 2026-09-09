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
