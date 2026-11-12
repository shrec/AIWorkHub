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

import base64
import dataclasses
import hashlib
import hmac
import json
import math
import re
import threading
from collections import OrderedDict
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .known_bug_scanner import _canonical_receipt_bytes

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

# Root labels of the sealed-tree manifest (NF-2026-01953).
SEALED_ROOT = "sealed"
CANONICAL_ROOT = "canonical"
SEALED_TREE_MUTATED = "finding_reproduction_sealed_tree_mutated"
_MAX_MUTATION_ENTRIES = 8
_MAX_MUTATION_MESSAGE_CHARS = 2048
_MAX_MUTATION_PATH_CHARS = 256
_MAX_CONCURRENT_CHANGES = 32

# NF-2026-02095: wall-clock bound on one dispose-time reproduction, well under
# an MCP client's 1800 s idle timeout. Past it the dispose stores a signed
# UNRUNNABLE receipt with candidate detail ``timeout`` instead of hanging.
# Both are read at call time, so a test may monkeypatch them.
DISPOSE_REPRODUCTION_DEADLINE_SECONDS = 600.0
# How often a caller-supplied ``progress`` callback hears from a running
# reproduction while the dispose waits on it.
DISPOSE_PROGRESS_INTERVAL_SECONDS = 15.0
TIMEOUT_DETAIL = "timeout"
# At most this many dispose reproductions are in flight per process, awaited or
# abandoned alike (a run its last waiter abandoned at the deadline keeps
# running); past it a dispose gets a deadline receipt without starting a run.
DISPOSE_RUN_LIMIT = 4
# Signed receipts of abandoned runs that finished late, kept (oldest evicted)
# so a replay returns one at once instead of rerunning.
DISPOSE_FINISHED_RUN_LIMIT = 64

Runner = Callable[[Any, list[str]], list[dict[str, Any]]]
Manifest = Mapping[tuple[str, str], "str | None"]


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
        """The shell-free validation command the sandbox runner executes.

        A probe is one line: prelude plus body travel base64-encoded, so the
        command never carries a newline, ``;``, ``|``, backtick, ``<`` or ``>``
        that the validation tokenizer refuses (NF-2026-01985). The base64
        alphabet needs no quoting on POSIX or Windows.
        """
        if self.kind == "pytest":
            return f"python -m pytest -q -p no:cacheprovider {self.body}"
        encoded = base64.b64encode((_PROBE_PRELUDE + self.body).encode("utf-8"))
        return (
            "python -I -c "
            f"\"exec(__import__('base64').b64decode('{encoded.decode('ascii')}'))\""
        )


def _materialized_unknown() -> None:
    """No target-declared canonical materialization: every canonical change voids."""
    return None


@dataclass(frozen=True, slots=True)
class ReproductionTarget:
    """Where one spec runs: both scratch workspaces and the sealed-tree manifest.

    ``create_candidate``/``create_base`` each return one scratch workspace that
    ``cleanup`` disposes; ``run`` is the validation-sandbox runner with the
    ``run_validations`` shape. ``sealed_manifest(also)`` maps each guarded
    ``(root label, path)`` to the sha256 of its bytes (None when absent) -- see
    :func:`sealed_manifest`; ``also`` are earlier keys to re-hash.
    ``candidate_paths`` are guarded under every root. ``canonical_materialized``
    names, after both sides ran, the canonical working-tree paths either side
    copied into its workspace, or None when that is unknown.
    """

    base_oid: str
    create_candidate: Callable[[], Any]
    create_base: Callable[[], Any]
    cleanup: Callable[[Any], None]
    run: Runner
    sealed_manifest: Callable[[Iterable[tuple[str, str]]], Manifest]
    candidate_paths: tuple[str, ...] = ()
    canonical_materialized: Callable[[], Iterable[str] | None] = _materialized_unknown


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
        return _side(UNRUNNABLE, TIMEOUT_DETAIL, **facts)
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


def _mutation_message(
    keys: list[tuple[str, str]], before: Manifest, after: Manifest
) -> str:
    """``SEALED_TREE_MUTATED:<json>``: at most 8 entries, at most 2048 chars."""
    entries = [
        {
            "root": root,
            "path": path[:_MAX_MUTATION_PATH_CHARS],
            "before_sha256": before.get((root, path)),
            "after_sha256": after.get((root, path)),
        }
        for root, path in keys[:_MAX_MUTATION_ENTRIES]
    ]
    while True:
        message = f"{SEALED_TREE_MUTATED}:" + json.dumps(
            entries, separators=(",", ":"), sort_keys=True
        )
        if len(message) <= _MAX_MUTATION_MESSAGE_CHARS or len(entries) <= 1:
            return message
        entries.pop()


def _sealed_guard(
    before: Manifest, after: Manifest, target: ReproductionTarget
) -> tuple[list[str], int]:
    """Raise on a guarded change; return the concurrent canonical changes.

    A key only in ``after`` is a path a delta lister named only afterwards, so
    it changed during the run (its before hash is reported as null). Any change
    under a root other than the canonical one -- the sealed worktree -- or at a
    candidate path under any root raises. A canonical change elsewhere raises
    when a side materialized that path from the canonical working tree, or when
    that set is unknown; otherwise neither side read those bytes, and it is
    returned (sorted, at most 32 paths, plus the full count) for the receipt.
    """
    changed = sorted(
        key
        for key in set(before) | set(after)
        if key not in before or key not in after or before[key] != after[key]
    )
    if not changed:
        return [], 0
    candidate = set(target.candidate_paths)
    declared = target.canonical_materialized()
    materialized = None if declared is None else {str(path) for path in declared}
    guarded = [
        (root, path)
        for root, path in changed
        if root != CANONICAL_ROOT
        or path in candidate
        or materialized is None
        or path in materialized
    ]
    if guarded:
        raise RuntimeError(_mutation_message(guarded, before, after))
    return [path for _root, path in changed][:_MAX_CONCURRENT_CHANGES], len(changed)


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
    """Run ``spec`` on the candidate, and on its base only if the candidate failed.

    :func:`classify` reads the base outcome only when the candidate outcome is
    FAILED, so any other candidate outcome records the base as UNRUNNABLE with
    detail ``base_not_run:candidate_<outcome>`` and duration 0 without creating
    a base workspace (NF-2026-02076).

    Raises ``RuntimeError`` ``finding_reproduction_sealed_tree_mutated:<json>``,
    naming each guarded (root, path) and its before/after sha256, when the
    sealed candidate or guarded canonical bytes changed while the spec ran:
    such a run proves nothing and is never signed. A canonical change neither
    side read is recorded as ``concurrent_canonical_changes`` instead.
    """
    sealed_before = target.sealed_manifest(())
    candidate = _execute(spec, target.create_candidate, target.run, target.cleanup)
    if candidate["outcome"] != FAILED:
        base = _side(
            UNRUNNABLE,
            f"base_not_run:candidate_{candidate['outcome']}",
            duration_seconds=0,
        )
    elif target.base_oid:
        base = _execute(spec, target.create_base, target.run, target.cleanup)
    else:
        base = _side(UNRUNNABLE, "base_oid_missing")
    sealed_after = target.sealed_manifest(tuple(sealed_before))
    concurrent, concurrent_count = _sealed_guard(sealed_before, sealed_after, target)
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
    if concurrent:
        receipt["concurrent_canonical_changes"] = concurrent
        receipt["concurrent_canonical_change_count"] = concurrent_count
    return sign_receipt(receipt, coordinator_key)


def _deadline_seconds(value: object) -> float:
    """The positive, finite wall-clock bound; anything else is refused by name."""
    limit = DISPOSE_REPRODUCTION_DEADLINE_SECONDS if value is None else value
    if (
        isinstance(limit, bool)
        or not isinstance(limit, (int, float))
        or not math.isfinite(limit)
        or limit <= 0
    ):
        raise ValueError(f"finding_reproduction_deadline_invalid:{limit!r}"[:200])
    return float(limit)


def _positive_limit(value: object, name: str) -> int:
    """A positive int registry bound; anything else is refused by name."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"finding_reproduction_{name}_invalid:{value!r}"[:200])
    return value


def _wait_with_progress(
    finished: threading.Event,
    *,
    deadline_seconds: float,
    progress: Callable[[float, float], Any] | None,
) -> None:
    """Wait on ``finished`` at most ``deadline_seconds``, feeding ``progress``.

    ``progress`` is called every :data:`DISPOSE_PROGRESS_INTERVAL_SECONDS` with
    ``(elapsed_seconds, deadline_seconds)``; a failing progress sink is
    reporting only and never decides the outcome.
    """
    interval = DISPOSE_PROGRESS_INTERVAL_SECONDS
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not (
        math.isfinite(interval) and interval > 0
    ):
        interval = deadline_seconds
    started = time.monotonic()
    while True:
        remaining = deadline_seconds - (time.monotonic() - started)
        if remaining <= 0 or finished.wait(min(interval, remaining)):
            return
        if progress is not None:
            try:
                progress(round(time.monotonic() - started, 3), deadline_seconds)
            except Exception:  # noqa: BLE001 -- progress is reporting only
                pass


RunKey = tuple[str, str, str, str]


class _Run:
    """One in-flight reproduction; every field but ``finished`` changes under the registry lock.

    ``waiters`` counts callers still waiting on it; ``gave_up`` records that at
    least one waiter returned a deadline receipt, so the late result is kept
    for that caller's replay.
    """

    __slots__ = ("finished", "done", "waiters", "gave_up", "value", "error")

    def __init__(self) -> None:
        self.finished = threading.Event()
        self.done = False
        self.waiters = 0
        self.gave_up = False
        self.value: Any = None
        self.error: BaseException | None = None

    @property
    def abandoned(self) -> bool:
        """Still running with no caller left waiting on it."""
        return not self.done and self.waiters == 0


class DisposeRunRegistry:
    """Process-wide in-flight dispose reproductions, keyed by their binding.

    The key is ``(finding_id, spec sha256, candidate sha256, reviewer receipt
    sha256)``. A caller whose key already runs joins that run instead of
    starting another; a joiner's result passes the same ``accept`` check as a
    kept late result. A run is *abandoned* only once its last waiter gave up
    at the deadline; it keeps going (its sandbox timeout and ``_execute``
    cleanup still apply, nothing is killed), and when any waiter gave up its
    late signed result is kept in a bounded map
    (:data:`DISPOSE_FINISHED_RUN_LIMIT`, oldest evicted) for the next replay.
    At most :data:`DISPOSE_RUN_LIMIT` runs are in flight at once, awaited or
    abandoned; past it no run starts. A run that raised is never kept, so an
    error state is rechecked rather than cached. Waiters hold no lock while
    they wait, so concurrent disposes never serialize behind a run.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: dict[RunKey, _Run] = {}
        self._finished: OrderedDict[RunKey, Any] = OrderedDict()
        self._started = 0

    @property
    def started(self) -> int:
        """How many runs this registry has started."""
        with self._lock:
            return self._started

    def running_count(self) -> int:
        """Runs still in flight, abandoned or awaited."""
        with self._lock:
            return len(self._running)

    def abandoned_count(self) -> int:
        """In-flight runs whose last waiter gave up at the deadline."""
        with self._lock:
            return sum(1 for run in self._running.values() if run.abandoned)

    def run(
        self,
        key: RunKey,
        work: Callable[[], Any],
        *,
        deadline_seconds: float,
        progress: Callable[[float, float], Any] | None = None,
        accept: Callable[[Any], bool] = lambda _value: True,
    ) -> tuple[bool, Any]:
        """``(True, result)`` once ``key``'s run finished in time, else ``(False, None)``.

        A late result kept for ``key`` that ``accept`` admits is returned at
        once; a rejected one is dropped. A caller that joined a run another
        caller started gets its result only when ``accept`` admits it, else
        ``(False, None)``. An exception the run raised is re-raised here, on
        the waiting thread. ``(False, None)`` also comes back at once, without
        starting a run, when :data:`DISPOSE_RUN_LIMIT` runs are in flight.
        """
        run_limit = _positive_limit(DISPOSE_RUN_LIMIT, "run_limit")
        finished_limit = _positive_limit(DISPOSE_FINISHED_RUN_LIMIT, "finished_run_limit")
        with self._lock:
            if key in self._finished:
                late = self._finished[key]
                if accept(late):
                    self._finished.move_to_end(key)
                    return True, late
                del self._finished[key]
            run = self._running.get(key)
            start = run is None
            if run is None:
                if len(self._running) >= run_limit:
                    return False, None
                run = _Run()
                self._running[key] = run
                self._started += 1
            run.waiters += 1
        if start:
            try:
                threading.Thread(
                    target=self._work,
                    args=(key, run, work, finished_limit),
                    name="finding-reproduction",
                    daemon=True,
                ).start()
            except BaseException as exc:
                # No worker owns the run: settle it so a joined waiter returns.
                with self._lock:
                    if self._running.get(key) is run:
                        del self._running[key]
                    run.waiters -= 1
                    run.error, run.done = exc, True
                run.finished.set()
                raise
        _wait_with_progress(run.finished, deadline_seconds=deadline_seconds, progress=progress)
        with self._lock:
            run.waiters -= 1
            if not run.done:
                run.gave_up = True
                return False, None
        if run.error is not None:
            raise run.error
        if not start and not accept(run.value):
            return False, None
        return True, run.value

    def _work(
        self, key: RunKey, run: _Run, work: Callable[[], Any], finished_limit: int
    ) -> None:
        value: Any = None
        error: BaseException | None = None
        try:
            value = work()
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the waiting thread
            error = exc
        with self._lock:
            run.value, run.error, run.done = value, error, True
            if self._running.get(key) is run:
                del self._running[key]
            if run.gave_up and error is None:
                self._finished[key] = value
                self._finished.move_to_end(key)
                while len(self._finished) > finished_limit:
                    self._finished.popitem(last=False)
        run.finished.set()


_DISPOSE_RUNS = DisposeRunRegistry()


def deadline_receipt(
    spec: ReproductionSpec,
    *,
    finding_id: str,
    candidate_sha256: str,
    reviewer_receipt_sha256: str,
    coordinator_key: bytes,
    deadline_seconds: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The signed UNRUNNABLE receipt of a reproduction that outran its deadline.

    The candidate side is ``timeout`` (``timed_out`` true), the base side was
    never awaited, and ``deadline_seconds`` marks the receipt as a deadline
    receipt. The target may never have resolved, so ``base_oid`` is empty.
    UNRUNNABLE is no counter-evidence, so a dismissal still needs the
    manager's own.
    """
    receipt = {
        "schema_id": SCHEMA_ID,
        "finding_id": str(finding_id),
        "classification": UNRUNNABLE,
        "spec_kind": spec.kind,
        "spec_sha256": spec.sha256,
        "candidate_sha256": str(candidate_sha256),
        "reviewer_receipt_sha256": str(reviewer_receipt_sha256),
        "base_oid": "",
        "candidate": _side(
            UNRUNNABLE,
            TIMEOUT_DETAIL,
            timed_out=True,
            duration_seconds=round(float(deadline_seconds), 3),
        ),
        "base": _side(
            UNRUNNABLE, f"base_not_run:candidate_{UNRUNNABLE}", duration_seconds=0
        ),
        "deadline_seconds": float(deadline_seconds),
        "executed_at": (now or datetime.now(timezone.utc)).isoformat(),
    }
    return sign_receipt(receipt, coordinator_key)


def is_deadline_receipt(receipt: object) -> bool:
    """Whether ``receipt`` records a reproduction that outran its deadline."""
    candidate = receipt.get("candidate") if isinstance(receipt, Mapping) else None
    return (
        isinstance(receipt, Mapping)
        and receipt.get("classification") == UNRUNNABLE
        and receipt.get("deadline_seconds") is not None
        and isinstance(candidate, Mapping)
        and candidate.get("detail") == TIMEOUT_DETAIL
    )


def dispose_reproducer(
    *,
    coordinator_key: Callable[[], bytes],
    resolve_target: Callable[[ReproductionSpec], ReproductionTarget],
    deadline_seconds: float | None = None,
    progress: Callable[[float, float], Any] | None = None,
) -> Callable[..., dict[str, Any]]:
    """The ``reproduce`` callback ``ReviewOrchestrator.dispose_review_finding`` takes.

    A ``stored`` receipt that verifies under the coordinator key and binds to
    the same finding, spec, candidate and reviewer receipt is returned as is,
    so a replayed dispose never reruns the reproduction.

    NF-2026-02095: target resolution and the run are bounded by
    ``deadline_seconds`` (default :data:`DISPOSE_REPRODUCTION_DEADLINE_SECONDS`,
    read at call time) via the process-wide :class:`DisposeRunRegistry`, which
    feeds ``progress`` while it waits. Past the deadline the callback returns a
    :func:`deadline_receipt` instead of blocking the dispose. A stored deadline
    receipt is an error state, so it is rechecked rather than cached, but a
    recheck never starts a second run for the same binding: it joins the run
    still in flight (bounded by the deadline again), or returns the signed
    receipt that run produced after a waiter gave up on it. The stored receipt
    comes back only when the recheck times out again (or the joined run's
    receipt does not verify under the current key), which keeps a replay
    idempotent. With :data:`DISPOSE_RUN_LIMIT` runs in flight no new run
    starts at all.
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
        limit = _deadline_seconds(deadline_seconds)
        key = receipt_key(coordinator_key())
        binding = {
            "finding_id": finding_id,
            "candidate_sha256": candidate_sha256,
            "reviewer_receipt_sha256": reviewer_receipt_sha256,
        }
        reusable = verify_receipt(stored, key) and receipt_binds(
            stored, spec_text=spec_text, **binding
        )
        if reusable and not is_deadline_receipt(stored):
            return dict(stored or {})
        finished, receipt = _DISPOSE_RUNS.run(
            (str(finding_id), spec.sha256, str(candidate_sha256), str(reviewer_receipt_sha256)),
            lambda: reproduce_finding(
                spec, target=resolve_target(spec), coordinator_key=key, **binding
            ),
            deadline_seconds=limit,
            progress=progress,
            accept=lambda late: verify_receipt(late, key)
            and receipt_binds(late, spec_text=spec_text, **binding),
        )
        if finished:
            return receipt
        if reusable:
            return dict(stored or {})
        return deadline_receipt(
            spec, coordinator_key=key, deadline_seconds=limit, **binding
        )

    return _reproduce


def _path_sha256(root: Path, relative: str) -> str | None:
    """sha256 of one regular file beneath ``root``; None when absent or escaping."""
    path = (root / relative).resolve(strict=False)
    if (path == root or root in path.parents) and path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return None


def sealed_manifest(
    roots: Mapping[str, Path],
    paths: Iterable[str],
    *,
    deltas: Mapping[str, Iterable[Callable[[], Iterable[str]]]] | None = None,
    also: Iterable[tuple[str, str]] = (),
) -> dict[tuple[str, str], str | None]:
    """Per-(root label, path) sha256 of the exact bytes a reproduction must keep.

    Every root hashes ``paths``; each root also hashes every path its own
    ``deltas`` listers name at call time (that tree's currently-changed paths)
    and every ``also`` key under it -- the previous manifest's keys, so a path
    that left a delta is re-hashed rather than dropped. Absent paths hash to
    None; nothing else is covered. An unknown root label raises.
    """
    wanted = {label: {str(path) for path in paths} for label in roots}
    for label, listers in (deltas or {}).items():
        if label not in wanted:
            raise ValueError(f"sealed_manifest_root_unknown:{label}")
        for listed in listers:
            wanted[label].update(str(path) for path in listed())
    for label, relative in also:
        if label not in wanted:
            raise ValueError(f"sealed_manifest_root_unknown:{label}")
        wanted[label].add(str(relative))
    manifest: dict[tuple[str, str], str | None] = {}
    for label, root in roots.items():
        resolved = Path(root).resolve()
        for relative in sorted(wanted[label]):
            manifest[(label, relative)] = _path_sha256(resolved, relative)
    return manifest


def manager_reproducer(
    manager: Any,
    *,
    task_id: str,
    request_id: str,
    card: Mapping[str, Any],
    deadline_seconds: float | None = None,
    progress: Callable[[float, float], Any] | None = None,
) -> Callable[..., dict[str, Any]]:
    """Production wiring: the sandbox, workspaces and key of one ProcessManager.

    The candidate side is the combined validation tree (HEAD with the sparse
    definition disabled, plus the canonical delta, plus the sealed candidate
    overlay) that accept-time validation runs on, with an empty write scope
    (NF-2026-02077); the base side is a scratch workspace pinned to the
    candidate's ``base_oid``, its sparse definition disabled so it is the full
    base tree, with the same empty write scope. A failed expansion makes the
    base UNRUNNABLE, never an exception past ``_execute``. The sealed-tree
    guard hashes the candidate's changed paths in both trees plus each tree's
    own currently-changed paths: the sealed worktree's
    (``worker_workspace.changed_paths``) and the canonical repo's
    (``_canonical_worktree_delta_paths``). Any new modification outside
    .gitignore in the sealed worktree, or at a candidate path in either tree,
    raises ``finding_reproduction_sealed_tree_mutated``; so does a canonical
    one at a path the candidate side materialized from the canonical working
    tree. That set is the candidate workspace's ``workspace_baseline`` keys:
    ``create_combined_validation_workspace`` sets them to the seed baseline
    plus the canonical delta, the paths copied from the canonical working
    tree. The base side is pinned to ``base_oid``, which ``create_workspace``
    materializes from git objects alone (no live seed, overlay or rework
    copy), so it adds none. A candidate workspace that was never created or
    carries no baseline leaves the set unknown, and any canonical change then
    still raises. Nothing is resolved until a spec runs.
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
        # Sparse-tree plumbing (read-only inputs, the pytest node file) is gone:
        # the candidate side is a complete tree, so every spec file is present.
        base_card = {
            key: value for key, value in dict(card).items() if key != "rework_predecessor"
        }
        tag = uuid.uuid4().hex[:16]
        materialized: dict[str, frozenset[str] | None] = {}

        def _run(workspace: Any, commands: list[str]) -> list[dict[str, Any]]:
            return _pl._run_validations_with_toolchain_receipt(
                workspace,
                commands,
                {},
                **_pl._validation_route_kwargs({"adapter_id": adapter_id}),
            )

        def _create_candidate() -> Any:
            # The complete tree accept-time validation runs on: HEAD with the
            # sparse definition disabled, the canonical delta, then the candidate
            # overlay. It carries no write scope here, exactly as the base side.
            workspace = dataclasses.replace(
                _pl.create_combined_validation_workspace(
                    source, base_card, candidate_paths
                )[0],
                allowed_writes=(),
            )
            # Seed baseline plus canonical delta: the paths copied from the
            # canonical working tree.
            baseline = getattr(workspace, "workspace_baseline", None)
            materialized["candidate"] = (
                frozenset(str(path) for path in baseline)
                if isinstance(baseline, Mapping)
                else None
            )
            return workspace

        def _create_base() -> Any:
            workspace = dataclasses.replace(
                _pl.create_workspace(
                    source.repo,
                    f"repro_base_{tag}",
                    base_card,
                    adapter_id,
                    pinned_base_oid=source.base_oid,
                ),
                allowed_writes=(),
            )
            try:
                # ``create_workspace`` is sparse; expand it to the full base_oid
                # tree minus forbidden paths under the card's materialize/checkout
                # rule (one shared helper). A failure raises, which ``_execute``
                # records as an UNRUNNABLE base naming it.
                from . import worker_materialize

                worker_materialize.expand_full_tree(
                    workspace.path,
                    base_card,
                    source.repo,
                    run=_pl._worker_workspace._run,
                    timeout=_pl._worker_workspace.WORKTREE_CREATE_TIMEOUT_SECONDS,
                    failure=lambda detail: RuntimeError(
                        "finding_reproduction_base_sparse_disable_failed:" + detail[-300:]
                    ),
                )
            except Exception:
                _pl.cleanup_workspace(workspace.repo, workspace.path, workspace.home)
                raise
            return workspace

        return ReproductionTarget(
            base_oid=str(source.base_oid or ""),
            create_candidate=_create_candidate,
            create_base=_create_base,
            cleanup=lambda ws: _pl.cleanup_workspace(ws.repo, ws.path, ws.home),
            run=_run,
            sealed_manifest=lambda also: sealed_manifest(
                {SEALED_ROOT: source.path, CANONICAL_ROOT: source.repo},
                candidate_paths,
                deltas={
                    SEALED_ROOT: (lambda: _pl._worker_workspace.changed_paths(source),),
                    CANONICAL_ROOT: (
                        lambda: _pl._worker_workspace._canonical_worktree_delta_paths(
                            source.repo
                        ),
                    ),
                },
                also=also,
            ),
            candidate_paths=tuple(candidate_paths),
            # Absent key (candidate never created): unknown, never empty.
            canonical_materialized=lambda: materialized.get("candidate"),
        )

    return dispose_reproducer(
        coordinator_key=manager._terminal_authority_key,
        resolve_target=_target,
        deadline_seconds=deadline_seconds,
        progress=progress,
    )
