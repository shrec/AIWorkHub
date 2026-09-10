from __future__ import annotations

import ast
import fnmatch
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from . import callback_store
from . import db_writer
from .sqlite_readonly import connect_readonly


SCHEMA_ID = "aiworkhub.dependency_autolaunch_outcome.v1"

# NF-2026-00549 M4: the write-gate audit measured 3,047 launch-blocked
# entries over 40 days, re-issued in bursts of 4 per 200ms because every
# reconcile trigger re-attempted every denied launch identically.  A denial
# whose reason is deterministic for the current card configuration can only
# repeat, so it holds until the card row changes; anything else backs off
# exponentially instead of retrying on the next trigger.
#
# 2026-09-07: that vocabulary described the dominant denial class of its day
# and was never revisited.  Measured against the 286 launch_blocked events in
# the canonical store it engaged on 5 of them -- 1.7%.  It was also matched by
# bare substring containment, which fails in both directions: `runner_mismatch`
# did not cover `runner_adapter_mismatch` (a near-miss that silently cost
# coverage), while `topic_mismatch` did cover the unrelated
# `quality_review_binding_topic_mismatch` (an accidental claim).  Matching is
# now identifier-exact, and every reason a declared producer can mint must be
# either claimed here or explicitly disclaimed below --
# `test_every_launch_denial_reason_in_the_codebase_is_classified` fails on any
# reason that is neither, so a new denial can never again be silently absent.
#
# Classification rule -- read the code that RAISES the reason, never its name:
#   deterministic: every operand is a field of THIS card's row, so an identical
#                  relaunch can only reproduce the identical denial, and only a
#                  card-row change can clear it.
#   transient:     at least one operand lives outside the card row (another
#                  card's lifecycle, the filesystem, an installed executable, a
#                  live service), so a later identical attempt may succeed.
# Fail closed: a reason that cannot be PROVEN deterministic is disclaimed as
# transient.  Holding a transient denial strands a card that would have
# succeeded; leaving a deterministic one out only costs the retry it would
# have saved.
DETERMINISTIC_DENIAL_REASONS = frozenset(
    {
        # Card-identity write gate, core.py:1619-1636, reached on every
        # reconcile through claim_start_exact -> _canonical_write_gate.  Each
        # check compares the card row against itself.
        "card_scoped_task_unresolved",
        "card_scoped_identity_mismatch",
        # core.py:1621/1624/1630 -- one guard, _is_malformed_identity_token,
        # over three card fields.  Only `malformed_topic` had been claimed; its
        # two siblings were the same near-miss as runner_adapter_mismatch.
        "malformed_runner",
        "malformed_task_id",
        "malformed_topic",
        # Workforce route identity, resolved from the card's runner/model.
        "workforce_route_absent",
        "workforce_model_mismatch",
        "workforce_route_disabled",
        "workforce_route_unavailable",
        "workforce_route_risk_incapable",
        # process_launcher.py:2812-2837 _validate_adapter_identity is a total
        # pure function of (runner, adapter_id) over hardcoded allow-tuples:
        # no I/O, no clock, no external state.  `runner_adapter_mismatch` is
        # that same predicate and was missed only by prefix shape.
        "runner_mismatch",
        "runner_adapter_mismatch",
        "topic_mismatch",
        "identical_relaunch_blocked",
        # process_launcher.py:4085-4099.  Raised iff the card's topic and the
        # caller's binding disagree.  LaunchFn here is Callable[[str, str, str,
        # str], ...] and structurally cannot carry a binding, so a
        # quality_review card denies identically until its topic changes;
        # the documented recovery is launch_quality_reviewer, not a retry.
        "quality_review_binding_required",
        "quality_review_binding_topic_mismatch",
        # launch_replay_guard.py:25-83.  Every check compares two fields of the
        # SAME card (validation_only_replay_authorization against
        # rework_predecessor and claim_epoch).  No filesystem, no task events,
        # no service call -- the purest deterministic family in the set.
        "validation_only_replay_authorization_invalid",
        "validation_only_replay_episode_binding_missing",
        "validation_only_replay_task_mismatch",
        "validation_only_replay_actor_mismatch",
        "validation_only_replay_predecessor_missing",
        "validation_only_replay_predecessor_mismatch",
        "validation_only_replay_hash_manifest_mismatch",
        "validation_only_replay_hash_manifest_invalid",
        "validation_only_replay_claim_epoch_invalid",
        "validation_only_replay_claim_epoch_mismatch",
    }
)
# Reason families whose concrete suffix is minted by the producer.  A family
# matches an identifier equal to it or extending it across a `_` boundary.
DETERMINISTIC_DENIAL_FAMILIES: tuple[str, ...] = ("repo_policy",)

# Explicitly disclaimed: seen, read at the raise site, and NOT proven to depend
# only on this card's row.  Listing them is what makes drift visible -- a new
# reason belongs to neither set and fails the classification test.
#
# The seven measured production reasons deliberately left transient, with the
# event that clears them (none of which is a card-row change):
#   collision_guard_failed (62/286)  process_launcher.py:5454-5456.
#       task_plan.py:668-674 and 757-760 state it outright: a point-in-time
#       pre-claim result that must be re-projected, because "an
#       archived/finished contender leaves ready_capacity at zero forever even
#       though an exact launch would now pass its live guard".  It clears when
#       the OTHER card finishes.  Holding it would strand the largest bucket.
#   task_contract_unwinnable (18)    process_launcher.py:5436-5444.
#       AuthoritySnapshot.available is `not missing`, and `missing` is missing
#       executables/modules -- environment, not card.  repair() states it
#       "cannot invoke a package manager ... unresolved external requirements
#       remain unresolved", so installing the tool clears it without touching
#       the card.
#   workspace_required_input_missing (16)  worker_workspace.py:1233-1243.
#       Card-declared path AND repo filesystem existence.  A sibling card's
#       accept can promote the file into the canonical tree, clearing it.
#   unexpected_launch_error (16)     process_launcher.py:8552-8572.
#       The else-branch for exceptions NOT in the expected tuple: an
#       unbounded, unanticipated exception type.  Unknowable by construction.
#   quality_review_candidate_mismatch (16) worker_workspace.py:5158-5165.
#       Compares observed against retained candidate workspace content, which
#       changes under retention/restore without the card changing.
#   workspace_exists (8)             worker_workspace.py:4326-4331.
#       A leftover worktree directory.  Stranded-worktree recovery removes it
#       and never touches the card row, so a deterministic hold would outlive
#       the repair.
#   vscode_lm_initial_source_graph_prefetch_failed (8)
#       process_launcher.py:8022-8035.  A live Source Graph MCP call; the
#       server can be down, restarting or indexing.
TRANSIENT_DENIAL_REASONS = frozenset(
    {
        # -- measured in production; see the note above for why each is here.
        "collision_guard_failed",
        "task_contract_unwinnable",
        "workspace_required_input_missing",
        "unexpected_launch_error",
        "quality_review_candidate_mismatch",
        "workspace_exists",
        "vscode_lm_initial_source_graph_prefetch_failed",
        # -- card contract shape.  These read the card, but the launcher also
        # rewrites contract fields during preflight, and a repaired card is a
        # card-row change that releases any hold anyway; claiming them buys
        # nothing and risks stranding.
        "allow_empty_not_in_allowed_writes",
        "allow_empty_not_in_required_outputs",
        "allow_empty_required_outputs_invalid",
        "allow_empty_required_outputs_requires_required_outputs",
        "allow_unchanged_not_in_allowed_writes",
        "allow_unchanged_not_in_required_outputs",
        "allow_unchanged_required_outputs_invalid",
        "allow_unchanged_required_outputs_requires_required_outputs",
        "allowed_write_outside_repo",
        "allowed_writes_empty",
        "allowed_writes_invalid",
        "allowed_writes_missing",
        "contradictory_task_path_contract",
        "git_metadata_write_forbidden",
        "read_only_declaration_required",
        "required_output_not_allowed",
        "required_output_path_invalid",
        "required_outputs_invalid",
        # -- launch-identity DERIVATION (process_launcher.derive_launch_identity).
        # ``launch_identity_underivable`` fires when the card row carries no
        # runner/topic, and ``launch_adapter_underivable`` when no adapter in the
        # runner family's tuple passes repo policy for this card. Both read the
        # card, and both belong here for the same reason as the contract-shape
        # family above: the card row is repairable, and a repaired row releases
        # the hold anyway. The adapter one additionally quotes the repo-policy
        # verdict that refused each candidate, so a genuine ``repo_policy_*``
        # detail inside it is still claimed deterministic by the family rule --
        # which is exactly how the same policy denial is classified on its own.
        "launch_adapter_underivable",
        "launch_identity_underivable",
        # -- lifecycle and claim races.  Every one of these depends on who else
        # is holding the row right now, which is exactly what a retry resolves.
        "card_scoped_action_not_allowed",
        "card_scoped_claim_start_ineligible",
        "card_scoped_claimed_by_mismatch",
        "card_scoped_codex_forbidden",
        "card_scoped_launch_blocker_ineligible",
        "card_scoped_review_ineligible",
        "card_scoped_task_id_required",
        "card_scoped_usage_ineligible",
        "runner_and_topic_required_for_card_scoped_authority",
        "claim_receipt_invalid",
        "claim_start_failed",
        "concurrency_limit_reached",
        "coordinator_runner_cannot_launch_worker",
        "duplicate_live_task",
        "duplicate_persisted_task",
        "duplicate_reserved_task",
        "memory_launch_capacity_denied",
        "task_already_claimed",
        "task_claim_owner_mismatch",
        "task_identity_mismatch",
        "task_launch_already_attached",
        "task_lookup_failed",
        "task_lookup_invalid_json",
        "task_not_launchable",
        "task_not_unclaimed",
        # -- credentials, providers and live bridges: all resolved outside the
        # card, all restorable without touching it.
        "claude_authentication_unavailable",
        "deepseek_credential_missing",
        "deepseek_model_rejected",
        "deepseek_vscode_lm_unavailable",
        "glm_credential_missing",
        "glm_model_rejected",
        "glm_vscode_lm_unavailable",
        "grok_kilo_auth_unavailable",
        "grok_kilo_model_rejected",
        "vscode_lm_model_required",
        "vscode_lm_unavailable",
        "quality_review_source_graph_authority_unverified",
        "quality_review_source_graph_prewarm_failed",
        # -- the reviewer packet the LAUNCHER derives, not a card field.
        # process_launcher_launch_isolated.py:436-448 refuses a packet whose
        # candidate.scoped_audits carry any lens but the one being launched.
        # That operand is quality_reviewer.build_lens_packet's output over the
        # candidate's evidence, supplied through the caller's binding; a
        # correctly scoped rebuild clears it with no card-row change, so the
        # fail-closed rule above disclaims it rather than claiming it.
        "quality_review_packet_lens_scope_mismatch",
        # -- host, filesystem and external roots.
        "external_readonly_root_not_directory",
        "external_readonly_root_unavailable",
        "external_readonly_source_invalid",
        "external_readonly_source_not_absolute",
        "external_readonly_source_not_file_or_dir",
        "external_readonly_source_outside_roots",
        "external_readonly_source_unavailable",
        "external_readonly_sources_invalid",
        "external_readonly_sources_requires_deepseek_copilot_cli",
        "ledger_snapshot_unproven",
        "supervisor_pid_identity_unavailable",
        "windows_launch_cwd_unavailable",
        "worker_supervisor_script_missing",
        # -- replay grants that read task EVENTS or worker MCP gate receipts
        # rather than card fields.  Deliberately split from the card-pure
        # launch_replay_guard family claimed above: a terminal event or a gate
        # receipt can appear later with no card-row change.
        "validation_only_replay_committed_grant_mismatch",
        "validation_only_replay_predecessor_terminal_event_missing",
        "validation_only_replay_predecessor_worker_mcp_gate_missing",
        "validation_only_replay_predecessor_worker_mcp_gate_unsatisfied",
    }
)
TRANSIENT_BACKOFF_BASE_SECONDS = 5.0
TRANSIENT_BACKOFF_MAX_SECONDS = 300.0
SUCCESS_STATUSES = frozenset({"finished", "completed", "stale_already_done"})
SUCCESS_WORKER_STATUSES = frozenset({"done"})
FAILED_STATUSES = frozenset({"failed", "cancelled", "canceled"})
FAILED_WORKER_STATUSES = frozenset(
    {
        "failed",
        "cancelled",
        "canceled",
        "worker_failed",
        "validation_failed",
        "launch_failed",
    }
)
ACTIVE_STATUSES = frozenset({"pending"})
ACTIVE_WORKER_STATUSES = frozenset({"", "unclaimed"})


LaunchFn = Callable[[str, str, str, str], Mapping[str, Any]]


@dataclass(frozen=True)
class _TaskRow:
    task_id: str
    runner: str
    topic: str
    status: str
    worker_status: str
    card: dict[str, Any]
    updated_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_card(raw: object) -> dict[str, Any]:
    try:
        card = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        card = {}
    return card if isinstance(card, dict) else {}


def _depends_on(card: Mapping[str, Any]) -> list[str]:
    value = card.get("depends_on")
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        dep = str(item or "").strip()
        if dep and dep not in out:
            out.append(dep)
    return out


def _state(row: _TaskRow) -> str:
    status = (row.status or str(row.card.get("status") or "")).strip().lower()
    worker = (row.worker_status or str(row.card.get("worker_status") or "")).strip().lower()
    if status in SUCCESS_STATUSES or worker in SUCCESS_WORKER_STATUSES:
        return "success"
    if status in FAILED_STATUSES or worker in FAILED_WORKER_STATUSES:
        return "failed"
    if status == "review" or worker == "review":
        substatus = str(row.card.get("substatus") or "").strip().lower()
        if substatus in FAILED_WORKER_STATUSES or substatus == "dependency_blocked":
            return "failed"
        return "review"
    if status in {"processing", "in_progress"} or worker in {"claimed", "in_progress"}:
        return "processing"
    return "pending"


def _repo_db(repo_root: Path) -> Path:
    return Path(repo_root) / ".aiworkhub" / "tasking" / "task_queue.sqlite"


@contextmanager
def _write_connection(db: Path) -> Iterator[sqlite3.Connection]:
    with db_writer.write_lease(db):
        conn = sqlite3.connect(str(db), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def _read_connection(db: Path) -> sqlite3.Connection:
    conn = connect_readonly(db)
    conn.row_factory = sqlite3.Row
    return conn


def _read_rows(conn: sqlite3.Connection) -> dict[str, _TaskRow]:
    rows: dict[str, _TaskRow] = {}
    for row in conn.execute(
        "SELECT task_id, runner, topic, status, worker_status, card_json, updated_at "
        "FROM tasks WHERE COALESCE(archived_at, '') = ''"
    ):
        card = _load_card(row["card_json"])
        task_id = str(row["task_id"])
        rows[task_id] = _TaskRow(
            task_id=task_id,
            runner=str(row["runner"] or card.get("runner") or ""),
            topic=str(row["topic"] or card.get("topic") or ""),
            status=str(row["status"] or card.get("status") or ""),
            worker_status=str(row["worker_status"] or card.get("worker_status") or ""),
            card=card,
            updated_at=str(row["updated_at"] or ""),
        )
    return rows


def _ensure_holds_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dependency_autolaunch_holds ("
        "task_id TEXT PRIMARY KEY, reason TEXT NOT NULL, kind TEXT NOT NULL, "
        "attempts INTEGER NOT NULL, card_updated_at TEXT NOT NULL, "
        "next_attempt_at TEXT NOT NULL, recorded_at TEXT NOT NULL)"
    )


_REASON_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def denial_reason_tokens(reason: str) -> tuple[str, ...]:
    """Return the maximal identifier runs inside one denial string.

    A denial never arrives bare.  ``claim_start_exact`` wraps the write-gate
    verdict as ``"runner/topic allowlist denied: card_scoped_identity_mismatch"``
    and a launcher rejection appends detail as
    ``"runner_adapter_mismatch:runner=...:got=..."``.  The reason is therefore
    matched as a whole identifier anywhere in the string, never as a bare
    substring: substring containment is what let ``topic_mismatch`` silently
    claim the unrelated ``quality_review_binding_topic_mismatch`` while
    ``runner_mismatch`` silently failed to cover ``runner_adapter_mismatch``.
    """
    return tuple(_REASON_TOKEN_RE.findall(str(reason or "").strip().lower()))


def classify_denial(reason: str) -> str:
    """Classify one launch denial as ``deterministic`` or ``transient``.

    Fail closed.  Only a reason proven to depend solely on this card's row is
    deterministic; everything else -- including a reason nobody has classified
    yet -- backs off and is retried.  Holding a transient denial strands a card
    that would have succeeded, while leaving a deterministic one out costs only
    the retry it would have saved.
    """
    for token in denial_reason_tokens(reason):
        if token in DETERMINISTIC_DENIAL_REASONS:
            return "deterministic"
        for family in DETERMINISTIC_DENIAL_FAMILIES:
            if token == family or token.startswith(family + "_"):
                return "deterministic"
    return "transient"


def _denial_kind(reason: str) -> str:
    return classify_denial(reason)


# ---------------------------------------------------------------------------
# Anti-drift: the vocabulary above must not silently fall behind production.
#
# The producers below are the exact places a launch denial reason is minted.
# ``discover_launch_denial_reasons`` enumerates their literals straight from
# source so a test can refuse any reason this module neither claims nor
# disclaims.  Adding a producer means adding it here; adding a reason inside an
# existing producer is caught with no edit at all.
LAUNCH_DENIAL_RAISE_PRODUCERS: tuple[tuple[str, str], ...] = (
    # LaunchRejected exists only to deny a launch, wherever it is raised.
    ("*.py", "LaunchRejected"),
    # The validation-only replay grant fails closed with a plain ValueError.
    ("launch_replay_guard.py", "ValueError"),
)
# Write-gate authority returns its verdict as a dict rather than raising.
LAUNCH_DENIAL_DECISION_PRODUCERS: tuple[tuple[str, str], ...] = (
    ("core.py", "_check_card_scoped_write_authority"),
    ("core.py", "check_runner_topic_allowlist"),
)


def _static_head(node: ast.AST) -> str | None:
    """Return the leading static text of a reason expression, if it has one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _static_head(node.left)
    return None


def _reason_token(text: str) -> str | None:
    """Reduce ``reason:detail`` to its reason token.

    A head that ends in ``_`` is an f-string stem such as ``f"malformed_{label}"``:
    it names no single reason, so its concrete values are classified instead.
    """
    token = text.split(":", 1)[0].strip()
    return token if token and not token.endswith("_") else None


def _called_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    return getattr(func, "attr", "")


def discover_launch_denial_reasons(
    package_root: Path | str,
) -> dict[str, tuple[str, ...]]:
    """Return ``{reason_token: (file:line, ...)}`` for every declared producer."""
    root = Path(package_root)
    found: dict[str, list[str]] = {}

    def record(token: str, path: Path, lineno: int) -> None:
        found.setdefault(token, []).append(f"{path.name}:{lineno}")

    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        exception_names = {
            name
            for pattern, name in LAUNCH_DENIAL_RAISE_PRODUCERS
            if fnmatch.fnmatch(path.name, pattern)
        }
        if exception_names:
            for node in ast.walk(tree):
                if not isinstance(node, ast.Raise):
                    continue
                if not isinstance(node.exc, ast.Call) or not node.exc.args:
                    continue
                if _called_name(node.exc) not in exception_names:
                    continue
                head = _static_head(node.exc.args[0])
                token = _reason_token(head) if head else None
                if token:
                    record(token, path, node.lineno)
        decision_functions = {
            function
            for module, function in LAUNCH_DENIAL_DECISION_PRODUCERS
            if module == path.name
        }
        if not decision_functions:
            continue
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if function.name not in decision_functions:
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.Dict):
                    continue
                fields = {
                    key.value: value
                    for key, value in zip(node.keys, node.values)
                    if isinstance(key, ast.Constant)
                }
                allowed = fields.get("allowed")
                if not (isinstance(allowed, ast.Constant) and allowed.value is False):
                    continue
                reason = fields.get("reason")
                head = _static_head(reason) if reason is not None else None
                token = _reason_token(head) if head else None
                if token:
                    record(token, path, node.lineno)
    return {token: tuple(sites) for token, sites in sorted(found.items())}


def unclassified_denial_reasons(package_root: Path | str) -> dict[str, tuple[str, ...]]:
    """Return discovered reasons this module neither claims nor disclaims."""
    return {
        token: sites
        for token, sites in discover_launch_denial_reasons(package_root).items()
        if token not in DETERMINISTIC_DENIAL_REASONS
        and token not in TRANSIENT_DENIAL_REASONS
        and not any(
            token == family or token.startswith(family + "_")
            for family in DETERMINISTIC_DENIAL_FAMILIES
        )
    }


def _hold_for(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT reason, kind, attempts, card_updated_at, next_attempt_at "
        "FROM dependency_autolaunch_holds WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "reason": str(row["reason"]),
        "kind": str(row["kind"]),
        "attempts": int(row["attempts"]),
        "card_updated_at": str(row["card_updated_at"]),
        "next_attempt_at": str(row["next_attempt_at"]),
    }


def _record_denial(
    conn: sqlite3.Connection, child: _TaskRow, reason: str
) -> dict[str, Any]:
    prior = _hold_for(conn, child.task_id)
    attempts = (prior["attempts"] if prior is not None else 0) + 1
    kind = _denial_kind(reason)
    if kind == "deterministic":
        next_attempt_at = ""
    else:
        delay = min(
            TRANSIENT_BACKOFF_MAX_SECONDS,
            TRANSIENT_BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)),
        )
        next_attempt_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat()
    conn.execute(
        "INSERT INTO dependency_autolaunch_holds"
        "(task_id, reason, kind, attempts, card_updated_at, next_attempt_at, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(task_id) DO UPDATE SET reason=excluded.reason, "
        "kind=excluded.kind, attempts=excluded.attempts, "
        "card_updated_at=excluded.card_updated_at, "
        "next_attempt_at=excluded.next_attempt_at, recorded_at=excluded.recorded_at",
        (
            child.task_id,
            str(reason or "")[:240],
            kind,
            attempts,
            child.updated_at,
            next_attempt_at,
            _now(),
        ),
    )
    return {"kind": kind, "attempts": attempts, "next_attempt_at": next_attempt_at}


def _clear_hold(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute(
        "DELETE FROM dependency_autolaunch_holds WHERE task_id=?", (task_id,)
    )


def _request_id(parent_task_id: str, child_task_id: str) -> str:
    return f"dependency-autolaunch:{parent_task_id}:{child_task_id}"


def _mark_dependency_blocked(
    conn: sqlite3.Connection,
    child: _TaskRow,
    blocked_by: Iterable[str],
    trigger_task_id: str,
) -> bool:
    now = _now()
    blocked = sorted(set(blocked_by))
    card = dict(child.card)
    card.update(
        status="review",
        worker_status="review",
        substatus="dependency_blocked",
        dependency_blocked_by=blocked,
    )
    cur = conn.execute(
        "UPDATE tasks SET status='review', worker_status='review', card_json=?, updated_at=? "
        "WHERE task_id=? AND status='pending' AND worker_status='unclaimed'",
        (json.dumps(card, ensure_ascii=False), now, child.task_id),
    )
    if cur.rowcount != 1:
        return False
    conn.execute(
        "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) VALUES(?,?,?,?,?)",
        (
            child.task_id,
            "dependency_blocked",
            "codex",
            json.dumps(
                {
                    "trigger_task_id": trigger_task_id,
                    "blocked_by": blocked,
                    "schema_id": SCHEMA_ID,
                },
                ensure_ascii=False,
            ),
            now,
        ),
    )
    # dependency_blocked is still a terminal review outcome.  Enqueue its
    # manager wake in the same transaction instead of relying solely on the
    # dispatcher's repair scan.
    origin_thread_id = str(
        child.card.get("origin_thread_id") or ""
    ).strip()
    callback_store.enqueue_callback(
        conn,
        child.task_id,
        origin_thread_id,
        "blocked",
        provider=str(child.card.get("coordinator_provider") or "").strip().lower(),
        episode_id=str(child.card.get("claim_epoch") or 0),
    )
    return True


def reconcile(
    repo_root: Path | str,
    *,
    trigger_task_id: str = "",
    launch: LaunchFn,
    capacity: int | None = None,
) -> dict[str, Any]:
    """Launch newly-ready dependents through the canonical exact claim/start hook.

    The durable exact-once guard is the task row itself: children are updated
    from pending/unclaimed to processing/claimed only by ``launch``. Reloads,
    repeated accept hooks, and concurrent reconcilers therefore race on the
    canonical ``claim_start_exact`` compare-and-update instead of an auxiliary
    resolver state file.
    """
    root = Path(repo_root)
    db = _repo_db(root)
    outcome: dict[str, Any] = {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "repo_root": str(root),
        "trigger_task_id": trigger_task_id,
        "launched": [],
        "blocked": [],
        "delayed": [],
        "skipped": [],
    }
    if not db.exists():
        outcome["ok"] = False
        outcome["error"] = "task_db_missing"
        return outcome

    with _write_connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        callback_store.init_db(conn)
        _ensure_holds_table(conn)
        conn.commit()
        rows = _read_rows(conn)

    launched_count = 0
    for child in sorted(rows.values(), key=lambda r: r.task_id):
        deps = _depends_on(child.card)
        if not deps:
            continue
        child_state = _state(child)
        if child_state != "pending" or child.worker_status.strip().lower() not in ACTIVE_WORKER_STATUSES:
            outcome["skipped"].append({"task_id": child.task_id, "reason": f"not_pending_unclaimed:{child_state}"})
            continue
        dep_rows = [rows.get(dep) for dep in deps]
        missing = [dep for dep, dep_row in zip(deps, dep_rows) if dep_row is None]
        if missing:
            outcome["delayed"].append({"task_id": child.task_id, "reason": "missing_dependencies", "dependencies": missing})
            continue
        failed = sorted(dep.task_id for dep in dep_rows if dep is not None and _state(dep) == "failed")
        if failed:
            with _write_connection(db) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if _mark_dependency_blocked(conn, child, failed, trigger_task_id):
                    conn.commit()
                    blocked = True
                else:
                    conn.rollback()
                    blocked = False
            if blocked:
                outcome["blocked"].append({"task_id": child.task_id, "blocked_by": failed})
            else:
                outcome["delayed"].append({"task_id": child.task_id, "reason": "dependency_block_race"})
            continue
        waiting = [dep.task_id for dep in dep_rows if dep is not None and _state(dep) != "success"]
        if waiting:
            outcome["delayed"].append({"task_id": child.task_id, "reason": "waiting_dependencies", "dependencies": waiting})
            continue
        conn = _read_connection(db)
        try:
            hold = _hold_for(conn, child.task_id)
        finally:
            conn.close()
        if hold is not None:
            if child.updated_at and child.updated_at > hold["card_updated_at"]:
                # The card changed since the recorded denial: the hold no
                # longer describes this configuration, so it is released.
                with _write_connection(db) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    _clear_hold(conn, child.task_id)
                    conn.commit()
            elif hold["kind"] == "deterministic":
                outcome["skipped"].append(
                    {
                        "task_id": child.task_id,
                        "reason": "deterministic_denial_hold:"
                        + hold["reason"][:120],
                    }
                )
                continue
            elif hold["next_attempt_at"] > _now():
                outcome["delayed"].append(
                    {
                        "task_id": child.task_id,
                        "reason": "transient_backoff_hold",
                        "next_attempt_at": hold["next_attempt_at"],
                    }
                )
                continue
        if capacity is not None and launched_count >= capacity:
            outcome["delayed"].append({"task_id": child.task_id, "reason": "capacity"})
            continue
        result = dict(launch(child.task_id, child.runner, child.topic, _request_id(trigger_task_id, child.task_id)))
        if result.get("ok"):
            launched_count += 1
            with _write_connection(db) as conn:
                conn.execute("BEGIN IMMEDIATE")
                _clear_hold(conn, child.task_id)
                conn.commit()
            outcome["launched"].append({"task_id": child.task_id, "runner": child.runner, "topic": child.topic})
        else:
            denial = str(result.get("stderr") or result.get("error") or "")
            with _write_connection(db) as conn:
                conn.execute("BEGIN IMMEDIATE")
                hold_state = _record_denial(conn, child, denial)
                conn.commit()
            outcome["delayed"].append(
                {
                    "task_id": child.task_id,
                    "reason": "launch_not_claimed",
                    "stderr": denial[:240],
                    "denial_kind": hold_state["kind"],
                    "attempts": hold_state["attempts"],
                    "next_attempt_at": hold_state["next_attempt_at"],
                }
            )
    return outcome


def reconcile_after_accept(repo_root: Path | str, accepted_task_id: str, launch: LaunchFn) -> dict[str, Any]:
    return reconcile(repo_root, trigger_task_id=accepted_task_id, launch=launch)


def reconcile_startup(repo_root: Path | str, launch: LaunchFn, *, capacity: int | None = None) -> dict[str, Any]:
    return reconcile(repo_root, trigger_task_id="startup", launch=launch, capacity=capacity)
