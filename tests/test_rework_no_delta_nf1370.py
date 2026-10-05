"""NF-2026-01370 (a): a zero-delta rework is blocked, never ``review_ready``.

The measured defect on EntryLink 006a: a provider-relaunched rework attempt
that edited nothing still reached ``review_ready``.
``worker_workspace.validate_required_outputs`` counts the SEALED inherited
predecessor delta as a change -- correct for a ``validation_only_replay``,
where no provider ran and there is nothing new to require, and a false green
for a relaunched rework, which therefore arrived at the finalizer with the
PREDECESSOR's paths in ``changed`` and nothing of its own.

What is locked here:

* an attempt whose changed path SET and every per-path SHA-256 equal the
  rejected predecessor's ``changed_path_hashes`` is refused by name;
* one changed byte, one added path or one removed path is a real delta and is
  never refused, so a genuine rework still reaches ``review_ready``;
* ``validation_only_replay`` keeps its current outcome;
* the refusal terminalises ``worker_failed`` -- which the finalizer routes to
  the BLOCKED transition, not the review one -- and classifies as a candidate
  rework rather than a ``validation_environment`` fault, so nothing replays
  the identical bytes or blames the sandbox for work that was never done;
* the guard is wired into the real finalization path: it runs on the final
  ``changed`` set, after ``validate_required_outputs`` and before the
  worker-candidate ``review_ready`` transition it exists to prevent.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher as pl  # noqa: E402
from aiworkhub import process_launcher_evidence as evidence  # noqa: E402
from aiworkhub import terminal_failure_classification as tfc  # noqa: E402
from aiworkhub import worker_workspace as ww  # noqa: E402
from aiworkhub.process_launcher_acceptance import (  # noqa: E402
    changed_path_hashes,
)
from aiworkhub.worker_workspace import WorkerWorkspace, WorkspaceError  # noqa: E402

_PREDECESSOR = {
    "src/aiworkhub/alpha.py": "def alpha():\n    return 1\n",
    "tests/test_alpha.py": "def test_alpha():\n    assert True\n",
}


def _seed_rework_workspace(tmp_path: Path, contents: dict[str, str]) -> WorkerWorkspace:
    """A real seeded rework worktree with real baselines on real bytes.

    The baselines are the shape a rework launch produces: the predecessor's
    delta is already in the worktree (so ``workspace_baseline`` records it as
    the retry baseline) over a canonical parent that never had those bytes.
    """
    repo, work, home = (tmp_path / name for name in ("repo", "work", "home"))
    for base in (repo, work, home):
        base.mkdir(parents=True, exist_ok=True)
    parent_baseline: dict[str, str | None] = {}
    workspace_baseline: dict[str, str | None] = {}
    for relative, text in contents.items():
        target = work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        parent_baseline[relative] = None
        workspace_baseline[relative] = ww._hash_path(target)
    return WorkerWorkspace.from_metadata({
        "request_id": "nf1370-successor",
        "repo": str(repo),
        "path": str(work),
        "home": str(home),
        "allowed_writes": list(contents),
        "parent_baseline": parent_baseline,
        "workspace_baseline": workspace_baseline,
        "inherited_rework_paths": list(contents),
        "base_oid": "0" * 40,
    })


def _metadata(
    workspace: WorkerWorkspace,
    sealed: dict,
    *,
    execution_mode: str = "rework",
) -> dict:
    return {
        "task_id": "AIWORKHUB_NF1370",
        "runner": "codex",
        "topic": "task_lifecycle",
        "execution_mode": execution_mode,
        "claim_epoch": 7,
        "rework_predecessor": {
            "request_id": "nf1370-predecessor",
            "task_id": "AIWORKHUB_NF1370",
            "claim_epoch": 6,
            "changed_path_hashes": dict(sealed),
        },
        "workspace": workspace.as_metadata(),
    }


def _refuse(workspace, metadata, changed, *, replay: bool = False) -> str:
    return evidence.rework_no_delta_refusal(
        workspace, metadata, changed, validation_only_replay=replay
    )


def test_zero_delta_rework_is_refused_blocked_and_never_review_ready(tmp_path):
    workspace = _seed_rework_workspace(tmp_path, _PREDECESSOR)
    changed = sorted(_PREDECESSOR)
    sealed = changed_path_hashes(workspace, changed)
    # The sealed identity really is the predecessor's: hashed by the same
    # owner the retention path seals with, over the same bytes.
    assert set(sealed) == set(changed)
    assert all(isinstance(digest, str) for digest in sealed.values())

    refusal = _refuse(workspace, _metadata(workspace, sealed), changed)
    assert refusal.startswith(evidence.REWORK_NO_DELTA + ":")
    for relative in changed:
        assert relative in refusal

    terminal_state = pl._terminal_state_for_workspace_error(WorkspaceError(refusal))
    assert terminal_state == "worker_failed"
    assert terminal_state != "review_ready"
    # The candidate bytes stay sealed for the next rework attempt.
    assert terminal_state in evidence.DELTA_RETAINING_TERMINAL_STATES
    # ``finalize_failed`` is what this must NOT be: the learning taxonomy
    # reads that substatus as a validation-environment fault.
    assert terminal_state not in tfc.VALIDATION_ENVIRONMENT_TERMINAL_SUBSTATUSES
    assert "finalize_failed" in tfc.VALIDATION_ENVIRONMENT_TERMINAL_SUBSTATUSES

    disposition = tfc.failure_disposition_from_substatus(
        terminal_substatus=terminal_state, reason=refusal
    )
    assert disposition["cause"] == tfc.CAUSE_CANDIDATE_CODE
    assert disposition["cause"] != tfc.CAUSE_VALIDATION_ENVIRONMENT
    assert disposition["cause_owner"] == tfc.CAUSE_OWNER_CANDIDATE
    assert disposition["action"] == tfc.ACTION_CANDIDATE_REWORK
    assert disposition["action"] != tfc.ACTION_VALIDATION_ONLY_REPLAY
    assert disposition["evidence_authority"] == tfc.EVIDENCE_AUTHORITY_AIWORKHUB_REASON


def test_one_changed_byte_added_or_removed_path_is_a_real_delta(tmp_path):
    workspace = _seed_rework_workspace(tmp_path, _PREDECESSOR)
    changed = sorted(_PREDECESSOR)
    sealed = changed_path_hashes(workspace, changed)
    metadata = _metadata(workspace, sealed)

    # One changed byte in one path.
    edited = workspace.path / changed[0]
    edited.write_text(_PREDECESSOR[changed[0]] + "# reworked\n", encoding="utf-8")
    assert _refuse(workspace, metadata, changed) == ""

    # An added path, with the inherited bytes untouched.
    edited.write_text(_PREDECESSOR[changed[0]], encoding="utf-8")
    assert _refuse(workspace, metadata, changed) != ""
    added = workspace.path / "src/aiworkhub/beta.py"
    added.write_text("def beta():\n    return 2\n", encoding="utf-8")
    assert _refuse(workspace, metadata, changed + ["src/aiworkhub/beta.py"]) == ""

    # A removed path: the attempt no longer carries one of the sealed paths.
    assert _refuse(workspace, metadata, changed[:1]) == ""


def test_validation_only_replay_with_no_new_delta_keeps_its_outcome(tmp_path):
    workspace = _seed_rework_workspace(tmp_path, _PREDECESSOR)
    changed = sorted(_PREDECESSOR)
    sealed = changed_path_hashes(workspace, changed)

    replay_metadata = _metadata(
        workspace, sealed, execution_mode="validation_only_replay"
    )
    assert _refuse(workspace, replay_metadata, changed, replay=True) == ""
    # The flag the finalizer derives from ``execution_mode`` is what decides
    # it, so an authorized replay of the exact retained candidate is never
    # refused for having no new delta -- that is the point of a replay.
    assert replay_metadata["execution_mode"] == "validation_only_replay"


def test_absent_or_unusable_predecessor_identity_mints_no_refusal(tmp_path):
    workspace = _seed_rework_workspace(tmp_path, _PREDECESSOR)
    changed = sorted(_PREDECESSOR)
    sealed = changed_path_hashes(workspace, changed)

    # Not a rework at all.
    first_attempt = _metadata(workspace, sealed)
    first_attempt.pop("rework_predecessor")
    assert evidence.is_rework_attempt(first_attempt) is False
    assert _refuse(workspace, first_attempt, changed) == ""

    # A rework whose predecessor record published no path identity.
    for unusable in ({}, None, [], {"src/aiworkhub/alpha.py": None}):
        metadata = _metadata(workspace, sealed)
        metadata["rework_predecessor"]["changed_path_hashes"] = unusable
        assert _refuse(workspace, metadata, changed) == ""


def _finalizer() -> ast.FunctionDef:
    tree = ast.parse(Path(pl.__file__).read_text(encoding="utf-8"), pl.__file__)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ProcessManager":
            for item in node.body:
                if (
                    isinstance(item, ast.FunctionDef)
                    and item.name == "_finalize_isolated_request"
                ):
                    return item
    raise AssertionError("ProcessManager._finalize_isolated_request not found")


def _call_lines(scope: ast.AST, name: str) -> list[int]:
    lines = []
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == name:
            lines.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == name:
            lines.append(node.lineno)
    return sorted(lines)


def _attribute_calls(statements) -> set[str]:
    return {
        node.func.attr
        for statement in statements
        for node in ast.walk(statement)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_finalizer_runs_the_guard_before_the_candidate_review_ready_transition():
    """The guard is in the REAL finalization path, not only a helper."""
    finalizer = _finalizer()

    guard = _call_lines(finalizer, "_rework_no_delta_refusal")
    assert len(guard) == 1, "the finalizer must consult the guard exactly once"
    validated = _call_lines(finalizer, "validate_required_outputs")
    assert len(validated) == 1
    # The guard must see the FINAL changed set, which includes the mandatory
    # outputs validate_required_outputs admitted -- the sealed inherited delta
    # is exactly what arrives through there.
    assert validated[0] < guard[0]

    raised = [
        node.lineno
        for node in ast.walk(finalizer)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "WorkspaceError"
        and node.exc.args
        and isinstance(node.exc.args[0], ast.Name)
        and node.exc.args[0].id == "rework_refusal"
    ]
    assert len(raised) == 1
    assert guard[0] < raised[0]

    review_ready = sorted(
        node.lineno
        for node in ast.walk(finalizer)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and node.value.value == "review_ready"
        and any(
            isinstance(target, ast.Name) and target.id == "terminal_state"
            for target in node.targets
        )
    )
    assert review_ready, "the finalizer no longer transitions to review_ready"
    # The worker-candidate transition is the last one in the method body; the
    # refusal is raised before it can ever be reached.
    assert raised[0] < review_ready[-1]


def test_finalizer_routes_worker_failed_to_the_blocked_transition():
    """``worker_failed`` must take the blocking transition, not the review one."""
    finalizer = _finalizer()
    candidates = [
        node
        for node in ast.walk(finalizer)
        if isinstance(node, ast.If)
        and "_terminal_failure_exact" in _attribute_calls(node.body)
        and "_review_terminal_exact" in _attribute_calls(node.orelse)
    ]
    assert candidates, "terminal routing branch not found"
    # An ENCLOSING if/else holds both transitions incidentally (the failure
    # arm and the clean-exit arm), so the branch that actually chooses
    # between them for a WorkspaceError outcome is the innermost match.
    routing = min(candidates, key=lambda node: node.end_lineno - node.lineno)
    routed = {
        node.value
        for node in ast.walk(routing.test)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {"finalize_failed", "worker_failed"} <= routed
