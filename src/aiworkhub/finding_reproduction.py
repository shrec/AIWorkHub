"""Coordinator-executed reproduction of one reviewer finding (RM-2026-00083).

A reviewer may put an executable reproduction spec in a finding's
``reproduction`` field:

* ``pytest:<node id>`` -- one pytest node under ``tests/``, no options;
* ``probe:<python source>`` -- at most :data:`MAX_PROBE_BYTES` bytes of source,
  run with ``python -I -c`` from the tree root with ``src`` on the path.

Anything else stays free text and is never executed. On a manager dispose the
coordinator runs the spec in the validation sandbox on a scratch copy of the
sealed candidate and on a scratch workspace pinned to the candidate's
``base_oid``, classifies the pair mechanically and signs the result with its
own key. A reviewer can only propose a reproduction: the evidence level
``reproduced`` is assigned from this receipt, never from reviewer text.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import re
import shlex
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .known_bug_scanner import _canonical_receipt_bytes, _source_revision_sha256

SCHEMA_ID = "aiworkhub.finding_reproduction_receipt.v1"
PYTEST_PREFIX = "pytest:"
PROBE_PREFIX = "probe:"
MAX_PROBE_BYTES = 4000
MAX_PYTEST_NODE_CHARS = 400
# Prefixed onto reviewer text whose truncation would otherwise read as a spec.
UNEXECUTED_PREFIX = "unexecuted:"

REPRODUCED = "reproduced"
PRE_EXISTING = "pre_existing"
NOT_REPRODUCED = "not_reproduced"
UNRUNNABLE = "unrunnable"
CLASSIFICATIONS = frozenset({REPRODUCED, PRE_EXISTING, NOT_REPRODUCED, UNRUNNABLE})
# A dismissal may rest on these alone: the candidate does not fail the spec, or
# fails it exactly as its base already did.
COUNTER_EVIDENCE_CLASSIFICATIONS = frozenset({NOT_REPRODUCED, PRE_EXISTING})

PASSED = "passed"
FAILED = "failed"

_PYTEST_NODE_RE = re.compile(
    r"tests/(?:[A-Za-z0-9_][A-Za-z0-9_.-]*/)*[A-Za-z0-9_][A-Za-z0-9_.-]*\.py"
    r"(?:::[A-Za-z_][A-Za-z0-9_]*){0,3}(?:\[[A-Za-z0-9_.,=-]{1,200}\])?"
)
# ``-I`` drops PYTHONPATH and the implicit cwd entry, so the probe names the
# tree's ``src`` itself; the probe runs with the tree root as cwd. The hook
# keeps exit 1 for an uncaught AssertionError only (an explicit ``sys.exit(1)``
# never reaches it): any other uncaught exception -- an ImportError on a base
# that lacks the candidate's module, say -- exits 3, which is unrunnable.
_PROBE_PRELUDE = (
    "import os, sys, traceback\n"
    "def _aiworkhub_probe_excepthook(kind, value, tb):\n"
    "    traceback.print_exception(kind, value, tb)\n"
    "    sys.stdout.flush()\n"
    "    sys.stderr.flush()\n"
    "    os._exit(1 if issubclass(kind, AssertionError) else 3)\n"
    "sys.excepthook = _aiworkhub_probe_excepthook\n"
    "sys.path.insert(0, 'src')\n"
)
_MAX_DETAIL_CHARS = 200

Runner = Callable[[Any, list[str]], list[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ReproductionSpec:
    """One parsed spec; ``text`` is the exact reviewer string that was parsed."""

    kind: str
    text: str
    body: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def command(self) -> str:
        """The shell-free validation command the sandbox runner executes."""
        if self.kind == "pytest":
            return f"python -m pytest -q -p no:cacheprovider {self.body}"
        return "python -I -c " + shlex.quote(_PROBE_PRELUDE + self.body)


@dataclass(frozen=True, slots=True)
class ReproductionTarget:
    """Where one spec runs: both scratch workspaces and the sealed-tree digest.

    ``create_candidate``/``create_base`` each return one scratch workspace that
    ``cleanup`` disposes; ``run`` is the validation-sandbox runner with the
    ``run_validations`` shape; ``sealed_digest`` hashes the path set and bytes
    a reproduction must never change -- see :func:`sealed_digest` for exactly
    which paths of the sealed candidate and canonical trees that is.
    """

    base_oid: str
    create_candidate: Callable[[], Any]
    create_base: Callable[[], Any]
    cleanup: Callable[[Any], None]
    run: Runner
    sealed_digest: Callable[[], str]


def _compiles(source: str) -> bool:
    """Whether ``source`` compiles; nothing is executed.

    ``python -c`` reports a compile error with exit 1 before the prelude's hook
    is installed, which would read as a failed probe, so such text is no spec.
    """
    try:
        compile(source, "<probe>", "exec", dont_inherit=True)
    except Exception:  # noqa: BLE001 -- SyntaxError, ValueError, RecursionError
        return False
    return True


def parse_reproduction_spec(value: object) -> ReproductionSpec | None:
    """Return the executable spec in ``value``, or None for free text."""
    if not isinstance(value, str):
        return None
    if value.startswith(PYTEST_PREFIX):
        node = value[len(PYTEST_PREFIX):]
        if len(node) <= MAX_PYTEST_NODE_CHARS and _PYTEST_NODE_RE.fullmatch(node):
            return ReproductionSpec("pytest", value, node)
        return None
    if value.startswith(PROBE_PREFIX):
        source = value[len(PROBE_PREFIX):]
        try:
            size = len(source.encode("utf-8"))
        except UnicodeEncodeError:
            return None
        if (
            source.strip()
            and size <= MAX_PROBE_BYTES
            and "\x00" not in source
            and _compiles(_PROBE_PRELUDE + source)
        ):
            return ReproductionSpec("probe", value, source)
    return None


def bounded_reproduction_text(raw: object, limit: int) -> str:
    """Bound a reviewer ``reproduction`` field without altering a spec.

    A parseable spec is kept whole, since its exact bytes are what runs. Other
    text is cut to ``limit``; a cut that would itself parse as a spec (an
    oversize probe, say) is prefixed so truncation can never manufacture
    source the reviewer did not submit.
    """
    text = str(raw or "")
    if parse_reproduction_spec(text) is not None:
        return text
    bounded = text[:limit]
    if parse_reproduction_spec(bounded) is not None:
        return (UNEXECUTED_PREFIX + bounded)[:limit]
    return bounded


def report_has_spec(report: object) -> bool:
    """Whether any finding in one reviewer report carries a parseable spec."""
    findings = report.get("findings") if isinstance(report, Mapping) else None
    return any(
        isinstance(row, Mapping)
        and parse_reproduction_spec(row.get("reproduction")) is not None
        for row in (findings if isinstance(findings, list) else [])
    )


def classify(candidate: str, base: str) -> str:
    """Mechanical classification from the two side outcomes."""
    if candidate == PASSED:
        return NOT_REPRODUCED
    if candidate != FAILED:
        return UNRUNNABLE
    if base == PASSED:
        return REPRODUCED
    if base == FAILED:
        return PRE_EXISTING
    return UNRUNNABLE


def is_counter_evidence(receipt: Mapping[str, Any] | None) -> bool:
    return (
        isinstance(receipt, Mapping)
        and receipt.get("classification") in COUNTER_EVIDENCE_CLASSIFICATIONS
    )


def receipt_key(coordinator_key: bytes) -> bytes:
    """Per-schema subkey of the coordinator's private HMAC key.

    The same owner-only coordinator key family signs terminal grants; the
    subkey keeps a signature of one schema from ever verifying as another.
    """
    if not coordinator_key:
        raise ValueError("coordinator_key must be non-empty")
    return hmac.new(coordinator_key, SCHEMA_ID.encode("utf-8"), hashlib.sha256).digest()


def sign_receipt(receipt: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    if not key:
        raise ValueError("coordinator_key must be non-empty")
    signed = {k: v for k, v in receipt.items() if k != "hmac_sha256"}
    signed["hmac_sha256"] = hmac.new(
        key, _canonical_receipt_bytes(signed), hashlib.sha256
    ).hexdigest()
    return signed


def verify_receipt(receipt: object, key: bytes) -> bool:
    if not key or not isinstance(receipt, Mapping) or receipt.get("schema_id") != SCHEMA_ID:
        return False
    signature = receipt.get("hmac_sha256")
    expected = hmac.new(
        key, _canonical_receipt_bytes(dict(receipt)), hashlib.sha256
    ).hexdigest()
    return isinstance(signature, str) and hmac.compare_digest(signature, expected)


def receipt_binds(
    receipt: object,
    *,
    finding_id: str,
    spec_text: str,
    candidate_sha256: str,
    reviewer_receipt_sha256: str,
) -> bool:
    """Whether ``receipt`` is a receipt for exactly this finding and spec."""
    return (
        isinstance(receipt, Mapping)
        and receipt.get("schema_id") == SCHEMA_ID
        and receipt.get("classification") in CLASSIFICATIONS
        and receipt.get("finding_id") == str(finding_id)
        and receipt.get("spec_sha256")
        == hashlib.sha256(str(spec_text).encode("utf-8")).hexdigest()
        and receipt.get("candidate_sha256") == str(candidate_sha256)
        and receipt.get("reviewer_receipt_sha256") == str(reviewer_receipt_sha256)
    )


def _side(outcome: str, detail: str = "", **facts: Any) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "returncode": facts.get("returncode"),
        "timed_out": bool(facts.get("timed_out")),
        "duration_seconds": facts.get("duration_seconds"),
        "detail": detail[:_MAX_DETAIL_CHARS],
    }


def _judge(rows: object, duration: float) -> dict[str, Any]:
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        return _side(UNRUNNABLE, "receipt_count_mismatch", duration_seconds=duration)
    row = rows[0]
    returncode = row.get("returncode")
    facts = {
        "returncode": returncode,
        "timed_out": row.get("timed_out"),
        "duration_seconds": duration,
    }
    if row.get("timed_out"):
        return _side(UNRUNNABLE, "timeout", **facts)
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        return _side(UNRUNNABLE, "returncode_missing", **facts)
    # 0 passed and 1 failed for both kinds: pytest's 2-5 are collection
    # errors, usage errors, a missing node or nothing collected. A probe exits
    # 1 only on an uncaught AssertionError or its own ``sys.exit(1)``; the
    # prelude's hook turns any other uncaught exception into 3, and python's
    # other codes are a usage error, an exit request or a sandbox refusal.
    if returncode == 0:
        return _side(PASSED, **facts)
    if returncode == 1:
        return _side(FAILED, **facts)
    return _side(UNRUNNABLE, f"exit_{returncode}", **facts)


def _execute(
    spec: ReproductionSpec,
    create: Callable[[], Any],
    run: Runner,
    cleanup: Callable[[Any], None],
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        workspace = create()
    except Exception as exc:  # noqa: BLE001 -- no workspace: nothing ran
        return _side(UNRUNNABLE, f"workspace_unavailable:{type(exc).__name__}:{exc}")
    try:
        try:
            rows = run(workspace, [spec.command])
        except Exception as exc:  # noqa: BLE001 -- a failing run still carries its rows
            rows = getattr(exc, "results", None)
            if not isinstance(rows, list) or not rows:
                return _side(
                    UNRUNNABLE,
                    f"sandbox_refused:{type(exc).__name__}:{exc}",
                    duration_seconds=round(time.perf_counter() - started, 3),
                )
        return _judge(rows, round(time.perf_counter() - started, 3))
    finally:
        cleanup(workspace)


def reproduce_finding(
    spec: ReproductionSpec,
    *,
    finding_id: str,
    candidate_sha256: str,
    reviewer_receipt_sha256: str,
    target: ReproductionTarget,
    coordinator_key: bytes,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run ``spec`` on the candidate and on its base; return a signed receipt.

    Raises ``RuntimeError`` when the sealed candidate or canonical bytes
    changed while the spec ran: such a run proves nothing and is never signed.
    """
    sealed_before = target.sealed_digest()
    candidate = _execute(spec, target.create_candidate, target.run, target.cleanup)
    base = (
        _execute(spec, target.create_base, target.run, target.cleanup)
        if target.base_oid
        else _side(UNRUNNABLE, "base_oid_missing")
    )
    if target.sealed_digest() != sealed_before:
        raise RuntimeError("finding_reproduction_sealed_tree_mutated")
    receipt = {
        "schema_id": SCHEMA_ID,
        "finding_id": str(finding_id),
        "classification": classify(candidate["outcome"], base["outcome"]),
        "spec_kind": spec.kind,
        "spec_sha256": spec.sha256,
        "candidate_sha256": str(candidate_sha256),
        "reviewer_receipt_sha256": str(reviewer_receipt_sha256),
        "base_oid": str(target.base_oid),
        "candidate": candidate,
        "base": base,
        "executed_at": (now or datetime.now(timezone.utc)).isoformat(),
    }
    return sign_receipt(receipt, coordinator_key)


def dispose_reproducer(
    *,
    coordinator_key: Callable[[], bytes],
    resolve_target: Callable[[ReproductionSpec], ReproductionTarget],
) -> Callable[..., dict[str, Any]]:
    """The ``reproduce`` callback ``ReviewOrchestrator.dispose_review_finding`` takes.

    A ``stored`` receipt that verifies under the coordinator key and binds to
    the same finding, spec, candidate and reviewer receipt is returned as is,
    so a replayed dispose never reruns the reproduction.
    """

    def _reproduce(
        *,
        finding_id: str,
        spec_text: str,
        candidate_sha256: str,
        reviewer_receipt_sha256: str,
        stored: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        spec = parse_reproduction_spec(spec_text)
        if spec is None:
            raise ValueError("finding_reproduction_spec_unparseable")
        key = receipt_key(coordinator_key())
        binding = {
            "finding_id": finding_id,
            "candidate_sha256": candidate_sha256,
            "reviewer_receipt_sha256": reviewer_receipt_sha256,
        }
        if verify_receipt(stored, key) and receipt_binds(
            stored, spec_text=spec_text, **binding
        ):
            return dict(stored or {})
        return reproduce_finding(
            spec, target=resolve_target(spec), coordinator_key=key, **binding
        )

    return _reproduce


def sealed_digest(
    roots: Iterable[Path],
    paths: Iterable[str],
    *,
    deltas: Iterable[Callable[[], Iterable[str]]] = (),
) -> str:
    """One digest over the path set and exact bytes under every root.

    The path set is ``paths`` plus every path each ``deltas`` lister names at
    call time (a tree's currently-changed paths), and each root hashes every
    path in it, absent ones included. So a modification or new file anywhere a
    lister sees in any root changes the digest; nothing else is covered.
    """
    relative_set = set(paths)
    for listed in deltas:
        relative_set.update(str(path) for path in listed())
    relative = sorted(relative_set)
    return hashlib.sha256(
        "\0".join(
            _source_revision_sha256(Path(root).resolve(), relative) for root in roots
        ).encode("ascii")
    ).hexdigest()


def manager_reproducer(
    manager: Any, *, task_id: str, request_id: str, card: Mapping[str, Any]
) -> Callable[..., dict[str, Any]]:
    """Production wiring: the sandbox, workspaces and key of one ProcessManager.

    The candidate side is a read-only review workspace materialized from the
    sealed candidate exactly as a reviewer saw it; the base side is a scratch
    workspace pinned to the candidate's ``base_oid`` with the same empty write
    scope, the pattern of ``compare_schema_mypy_baseline``. The sealed-tree
    guard covers the candidate's changed paths plus the currently-changed
    paths of the sealed worktree (``worker_workspace.changed_paths``) and of
    the canonical repo (``_canonical_worktree_delta_paths``), with their bytes
    in both trees: any new modification outside .gitignore in either tree
    raises ``finding_reproduction_sealed_tree_mutated``. Nothing is resolved
    until a spec runs.
    """
    # Re-bound from the ``process_launcher`` module object at call time, as in
    # :func:`process_launcher_accept_review.accept_review`.
    from . import process_launcher as _pl

    def _target(spec: ReproductionSpec) -> ReproductionTarget:
        prep = manager._prepared_quality_review(request_id, task_id)
        if not prep.get("ok"):
            raise RuntimeError(
                "finding_reproduction_candidate_unavailable:"
                + str(prep.get("error") or "unknown")[:200]
            )
        prepared = prep["prepared"]
        source = prepared["workspace"]
        candidate_paths = sorted(prepared["changed_hashes"])
        adapter_id = str(prepared.get("worker_adapter_id") or "")
        read_only = list(prepared.get("read_only_input_paths") or [])
        node_file = spec.body.split("::", 1)[0] if spec.kind == "pytest" else ""
        if node_file and node_file not in candidate_paths and node_file not in read_only:
            read_only.append(node_file)
        base_card = {
            key: value for key, value in dict(card).items() if key != "rework_predecessor"
        }
        tag = uuid.uuid4().hex[:16]

        def _run(workspace: Any, commands: list[str]) -> list[dict[str, Any]]:
            return _pl._run_validations_with_toolchain_receipt(
                workspace,
                commands,
                {},
                **_pl._validation_route_kwargs({"adapter_id": adapter_id}),
            )

        return ReproductionTarget(
            base_oid=str(source.base_oid or ""),
            create_candidate=lambda: _pl.create_quality_review_workspace(
                source, f"repro_{tag}", candidate_paths, adapter_id, read_only
            )[0],
            # The candidate side is a review workspace with no write scope; the
            # base side gets the same, so a spec that writes inside the tree
            # fails (or passes) identically on both and never reads as reproduced.
            create_base=lambda: dataclasses.replace(
                _pl.create_workspace(
                    source.repo,
                    f"repro_base_{tag}",
                    base_card,
                    adapter_id,
                    pinned_base_oid=source.base_oid,
                ),
                allowed_writes=(),
            ),
            cleanup=lambda ws: _pl.cleanup_workspace(ws.repo, ws.path, ws.home),
            run=_run,
            sealed_digest=lambda: sealed_digest(
                (source.path, source.repo),
                candidate_paths,
                deltas=(
                    lambda: _pl._worker_workspace.changed_paths(source),
                    lambda: _pl._worker_workspace._canonical_worktree_delta_paths(
                        source.repo
                    ),
                ),
            ),
        )

    return dispose_reproducer(
        coordinator_key=manager._terminal_authority_key, resolve_target=_target
    )
