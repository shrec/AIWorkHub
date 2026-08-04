"""Local Codex App Server callback bridge for durable terminal-event delivery.

Speaks the real installed Codex 0.144.1 App Server wire protocol over a
single ``codex app-server --listen stdio://`` subprocess: newline-delimited
JSON messages with NO top-level ``jsonrpc`` member (verified against the
installed binary's own JSON Schema export -- ``JSONRPCRequest``/
``JSONRPCNotification``/``JSONRPCResponse``/``JSONRPCError`` require only
``id``/``method``/``params`` or ``id``/``result`` or ``id``/``error``).
Sequence: ``initialize`` -> wait for its successful response ->
``initialized`` notification -> ``thread/resume`` for the bound origin
thread. ``thread/resume``'s own response (``result.thread.status``/
``result.thread.turns``) decides the delivery path (B407): idle ->
``turn/start`` (with ``cwd`` and a deterministic ``clientUserMessageId``)
-> wait for the matching ``turn/completed`` notification; active with
exactly one ``inProgress`` turn -> ``turn/steer`` onto that exact turn
(``expectedTurnId`` + the same deterministic ``clientUserMessageId``),
acknowledged by a matching ``TurnSteerResponse.turnId`` alone, never
waiting for ``turn/completed`` and never starting a second turn on an
already-active thread. Any other active-thread shape (notLoaded/
systemError/unrecognized status, zero or multiple inProgress turns, a
stale ``expectedTurnId``, or an ``activeTurnNotSteerable`` review/compact
turn) is a bounded deferral, not a parallel turn/start. Never calls
``codex thread status`` and never uses the nonexistent ``codex exec
--thread-id/--client-id/--no-remote`` flags.

The callback prompt contains only validated task_id, normalized terminal
state, and request/event id.  Worker output, error text, objectives, logs,
artifacts, full origin_thread_id, and any worker-controlled text are
forbidden.

Design contract (B343 / B376 / B384 / B416):
- Outbox: durable dedup by (task_id, transition, origin_thread_id, episode).
- Eligible terminal states: review_ready, blocked, launch_failed,
  validation_failed, scope_rejected, timed_out, cancelled.
- Never enqueue: pending, processing, done, reject.
- Busy-thread deferral (derived from App Server responses, not a CLI status).
- At-most-one wake per terminal transition per claim episode.
- Leases, retry/backoff, delivered/dead-letter states.
- B416: a busy/active-thread deferral (``BusyThreadError`` and its
  subclasses) is a durable, non-blocking, capped-backoff PARK, never a
  bounded retry -- it can never dead-letter and never consumes the
  dead-letter failure budget, which is reserved for genuine
  transport/protocol failures (see ``callback_store.py::defer_batch_busy``
  vs ``fail_batch_transient`` and ``callback_batches.not_before_at``).
- Run-once, daemon, status, and dry-run commands.
"""
from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import select
import socket as _socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import callback_store
from . import repository_state
from .app_server_mux import (
    SIDEBAND_REQUEST_DEADLINE_SECONDS as DEFAULT_SIDEBAND_TIMEOUT,
    SIDEBAND_RESPONSE_MAX_BYTES,
    connect_sideband_socket,
    default_sideband_dir,
    describe_sideband_owner_freshness,
    find_owning_sideband_instances,
)
from .route_identity import CoordinatorRouteKey, fail_on_incomplete_coordinator_route

CALLBACK_ELIGIBLE_STATES: frozenset[str] = frozenset({
    "review_ready",
    "blocked",
    "launch_failed",
    "validation_failed",
    "scope_rejected",
    "timed_out",
    "token_budget_exceeded",
    "output_budget_exceeded",
    "cancelled",
})

DEFAULT_LEASE_SECONDS = 2100
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_BACKOFF_BASE = 2.0
DEFAULT_RETRY_BACKOFF_MAX = 300.0
DEFAULT_APP_SERVER_TIMEOUT = 1800.0
# The lease must outlive the App Server timeout by a real margin: if a turn
# is genuinely still running (a long CEO review) when the lease would
# otherwise expire, a second bridge instance/restart must not steal and
# re-deliver the same batch out from under the first. No 60-second implicit
# timeout path exists anywhere in this module -- every AppServerClient call
# site takes an explicit timeout, defaulting to DEFAULT_APP_SERVER_TIMEOUT.
DEFAULT_LEASE_MARGIN_SECONDS = 300.0
SIDEBAND_APP_SERVER_TIMEOUT_SECONDS = 45.0
SIDEBAND_LEASE_SECONDS = 90
SIDEBAND_LEASE_MARGIN_SECONDS = 30.0
REDACTED_SUFFIX_LENGTH = 4


def _bound_repo_from_env_or_cwd() -> Path:
    canonical_raw = os.environ.get("AIWORKHUB_REPO_ROOT", "").strip()
    legacy_raw = os.environ.get("AIWORKHUB_REPO", "").strip()
    if canonical_raw:
        canonical = Path(canonical_raw).expanduser().resolve()
        if legacy_raw:
            legacy = Path(legacy_raw).expanduser().resolve()
            if legacy != canonical:
                raise RuntimeError(
                    "repo_root_env_mismatch:"
                    f"AIWORKHUB_REPO_ROOT={canonical}:"
                    f"AIWORKHUB_REPO={legacy}"
                )
        return canonical
    if legacy_raw:
        return Path(legacy_raw).expanduser().resolve()
    return repository_state.resolve_repository_root(require_manifest=False)


CALLBACK_CWD = str(_bound_repo_from_env_or_cwd())

# Env var overrides for CLI-configurable timeout/lease (see main()/
# resolve_bridge_settings()). CLI flags take precedence over these.
ENV_APP_SERVER_TIMEOUT_SECONDS = "AIWORKHUB_CALLBACK_APP_SERVER_TIMEOUT_SECONDS"
ENV_LEASE_SECONDS = "AIWORKHUB_CALLBACK_LEASE_SECONDS"
ENV_MAX_BATCH_MEMBERS = "AIWORKHUB_CALLBACK_MAX_BATCH_MEMBERS"
ENV_TRANSPORT = "AIWORKHUB_CALLBACK_TRANSPORT"

# Rejected transport surface: never invoked, never emulated.
_REJECTED_FLAGS = ("--thread-id", "--client-id", "--no-remote")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{0,128}$")
# B925: same bounded-identifier shape as app_server_mux.py's own
# ``_validate_repo_id`` -- duplicated locally (not imported) so this
# module's repo_id validation never depends on app_server_mux.py's
# internals staying in a particular shape, only on this contract.
_REPO_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _validate_sideband_repo_id(repo_id: str) -> str:
    """Fail-closed validation for the repository identity a sideband
    transport binds to. Never guesses, never falls back to a shared/
    global identity -- an unbound or malformed repo_id must abort
    construction, not silently produce a transport that could resolve a
    different repository's mux instance as its owner (B925)."""
    if not isinstance(repo_id, str):
        raise ValueError(f"repo_id must be a string, got {type(repo_id).__name__}")
    stripped = repo_id.strip()
    if not stripped or not _REPO_ID_RE.fullmatch(stripped):
        raise ValueError(f"repo_id must be a non-empty bounded identifier, got {repo_id!r}")
    return stripped


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(iso_timestamp: str) -> float | None:
    if not iso_timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, round((datetime.now(timezone.utc) - parsed).total_seconds(), 3))


def validate_lease_and_timeout(
    lease_seconds: float, app_server_timeout: float,
    *, margin_seconds: float = DEFAULT_LEASE_MARGIN_SECONDS,
) -> None:
    """Enforce lease > timeout + margin. Raises ValueError if violated so a
    misconfigured deployment fails fast at startup, never silently."""
    if lease_seconds <= 0:
        raise ValueError(f"lease_seconds must be positive, got {lease_seconds}")
    if app_server_timeout <= 0:
        raise ValueError(f"app_server_timeout must be positive, got {app_server_timeout}")
    required = app_server_timeout + margin_seconds
    if lease_seconds < required:
        raise ValueError(
            f"lease_seconds ({lease_seconds}) must be >= app_server_timeout "
            f"({app_server_timeout}) + margin ({margin_seconds}) = {required}"
        )


def resolve_bridge_settings(
    argv: list[str], env: dict[str, str] | None = None,
) -> tuple[list[str], float, int, int]:
    """Resolve (remaining_argv, app_server_timeout, lease_seconds,
    max_batch_members) from CLI flags (highest precedence), then env vars,
    then the safe defaults. Validates lease > timeout + margin before
    returning -- an invalid combination raises ValueError immediately
    rather than silently running with an unsafe pair.
    """
    env = env if env is not None else os.environ
    remaining = list(argv)

    def _take_flag(flag: str) -> str | None:
        if flag not in remaining:
            return None
        idx = remaining.index(flag)
        if idx + 1 >= len(remaining):
            raise ValueError(f"{flag} requires a value")
        value = remaining[idx + 1]
        del remaining[idx:idx + 2]
        return value

    timeout_raw = _take_flag("--app-server-timeout-seconds") or env.get(ENV_APP_SERVER_TIMEOUT_SECONDS)
    lease_raw = _take_flag("--lease-seconds") or env.get(ENV_LEASE_SECONDS)
    max_members_raw = _take_flag("--max-batch-members") or env.get(ENV_MAX_BATCH_MEMBERS)

    app_server_timeout = float(timeout_raw) if timeout_raw else DEFAULT_APP_SERVER_TIMEOUT
    lease_seconds = int(float(lease_raw)) if lease_raw else DEFAULT_LEASE_SECONDS
    max_batch_members = int(max_members_raw) if max_members_raw else 25

    validate_lease_and_timeout(lease_seconds, app_server_timeout)
    if max_batch_members <= 0:
        raise ValueError(f"max_batch_members must be positive, got {max_batch_members}")
    return remaining, app_server_timeout, lease_seconds, max_batch_members


def redacted_thread_suffix(thread_id: str | None, length: int = REDACTED_SUFFIX_LENGTH) -> str:
    if not thread_id or len(thread_id) < length:
        return "***"
    return "***" + thread_id[-length:]


# --- App Server command / prompt construction -------------------------------

def build_app_server_command(
    executable: str | list[str] = "codex", listen: str = "stdio://"
) -> list[str]:
    """Build the ``codex app-server --listen stdio://`` argv.

    ``executable`` may be a single binary path/name or an argv prefix list
    (tests spawn a non-executable-bit fake server as
    ``[sys.executable, path]``). This callback-only command is deliberately
    separate from the existing Codex worker adapter and its sandbox policy.
    """
    prefix = [executable] if isinstance(executable, str) else list(executable)
    if not prefix or not all(isinstance(item, str) and item for item in prefix):
        raise ValueError("app server executable prefix must be nonempty strings")
    return [*prefix, "app-server", "--listen", listen]


_CALLBACK_PROMPT_TEMPLATE = "Task MCP: {task_id} → {state}"


def _validate_callback_fields(
    task_id: str, state: str, event_id: str, request_id: str,
) -> tuple[str, str, str, str]:
    """Shared validation for one callback member's four safe fields.

    Only these four are ever allowed into a callback prompt -- worker
    output, error text, objectives, logs, artifacts, full origin_thread_id,
    and any worker-controlled text are structurally forbidden (no such
    parameter exists on this path at all).
    """
    safe_task_id = str(task_id).strip()
    safe_state = str(state).strip()
    safe_event_id = str(event_id).strip()
    safe_request_id = str(request_id).strip()
    if not _TASK_ID_RE.fullmatch(safe_task_id):
        raise ValueError("invalid callback task_id")
    if safe_state not in CALLBACK_ELIGIBLE_STATES:
        raise ValueError("invalid callback terminal state")
    if not _EVENT_ID_RE.fullmatch(safe_event_id):
        raise ValueError("invalid callback event_id")
    if not _EVENT_ID_RE.fullmatch(safe_request_id):
        raise ValueError("invalid callback request_id")
    return safe_task_id, safe_state, safe_event_id, safe_request_id


def build_callback_prompt(
    task_id: str,
    state: str,
    *,
    event_id: str = "",
    request_id: str = "",
) -> str:
    """Build the fixed coordinator callback prompt.

    Only validated task_id, normalized terminal state, and request/event id
    are interpolated. Worker output, error text, objectives, logs, artifacts,
    full origin_thread_id, and any worker-controlled text are forbidden.
    """
    safe_task_id, safe_state, safe_event_id, safe_request_id = _validate_callback_fields(
        task_id, state, event_id, request_id,
    )
    return _CALLBACK_PROMPT_TEMPLATE.format(
        task_id=safe_task_id,
        state=safe_state,
        event_id=safe_event_id,
        request_id=safe_request_id,
    )


_CALLBACK_BATCH_PROMPT_TEMPLATE = (
    "Task MCP: {count} tasks terminal → inspect review queue"
)


def build_batch_callback_prompt(members: list[dict[str, str]]) -> str:
    """Build the fixed coordinator callback prompt for one coalesced batch.

    ``members`` is a bounded list (see
    ``taskdb.DEFAULT_CALLBACK_BATCH_MAX_MEMBERS``) of
    ``{"task_id", "state", "event_id"?, "request_id"?}`` dicts, one per
    task reaching a terminal state in this batch. Every member is still
    validated through ``_validate_callback_fields`` (durable delivery gate
    unchanged -- only the user-facing rendered text is compact). Worker
    output, error text, objectives, logs, artifacts, full origin_thread_id,
    and any worker-controlled text remain forbidden.
    """
    if not members:
        raise ValueError("callback batch prompt requires at least one member")
    validated = [
        _validate_callback_fields(
            member.get("task_id", ""),
            member.get("state", ""),
            member.get("event_id", ""),
            member.get("request_id", ""),
        )
        for member in members
    ]
    if len(validated) == 1:
        task_id, state, _event_id, _request_id = validated[0]
        return _CALLBACK_PROMPT_TEMPLATE.format(task_id=task_id, state=state)
    return _CALLBACK_BATCH_PROMPT_TEMPLATE.format(count=len(members))


def deterministic_client_user_message_id(
    task_id: str, transition: str, origin_thread_id: str, episode_id: str
) -> str:
    """A stable, non-random id for one (task, transition, thread, episode).

    Deterministic (not ``uuid4``/time-based) so a bridge restart mid-delivery
    reissues the identical id instead of a fresh one, matching the outbox's
    own at-most-once-per-episode dedup semantics.
    """
    import hashlib

    raw = f"{task_id}:{transition}:{origin_thread_id}:{episode_id}"
    return "cbmsg_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def deterministic_batch_client_user_message_id(batch_id: str) -> str:
    """A stable, non-random id for one delivery batch.

    ``batch_id`` is itself already a durable identifier assigned once at
    batch-formation time and persisted in ``callback_batches`` (see
    ``callback_store.py::claim_pending_callback_batch``), so hashing it
    alone is sufficient: a bridge restart mid-delivery reclaims the SAME
    batch_id (never re-forms a new one for an already-leased batch) and
    therefore always reissues the identical clientUserMessageId.
    """
    import hashlib

    return "cbbatch_" + hashlib.sha256(batch_id.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class CallbackDeliveryResult:
    provider: str
    route: CoordinatorRouteKey
    state: str
    durable: bool
    delivered: bool
    action: str
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "route": self.route.as_dict(),
            "state": self.state,
            "durable": self.durable,
            "delivered": self.delivered,
            "action": self.action,
            "reason": self.reason,
        }




class AppServerError(RuntimeError):
    """Bounded failure from the Codex App Server subprocess."""


class BusyThreadError(AppServerError):
    """The origin thread is busy with another turn; defer, never parallel-start."""


class ActiveThreadSteerDeferralError(BusyThreadError):
    """Bounded-deferral condition around an active-thread turn/steer decision.

    Covers every shape that must never trigger a second ``turn/start``:
    an unrecognized/``notLoaded``/``systemError`` thread status, zero or
    multiple ``inProgress`` turns on an ``active`` thread, and any
    ``turn/steer`` rejection (a stale ``expectedTurnId`` -- the request
    fails server-side when it no longer matches the currently active
    turn -- or an ``activeTurnNotSteerable`` review/compact turn, per the
    installed App Server's own ``CodexErrorInfo`` schema). Subclasses
    ``BusyThreadError`` so it is handled identically: the whole batch
    requeues/dead-letters on the existing bounded retry path, the App
    Server client is never torn down, and no parallel turn is ever
    started.
    """
class ClaudeCliError(AppServerError):
    """Base for all Claude CLI resume-transport failures."""


class ClaudeCliBusyError(BusyThreadError):
    """The bound Claude session has an active turn; durable busy park,
    exactly like ``BusyThreadError``/``SidebandThreadBusyError`` -- never a
    parallel injected turn, never a dead-letter."""


class ClaudeCliUnavailableError(BusyThreadError):
    """No usable Claude CLI resume capability right now -- the ``claude``
    executable could not be spawned at all (``FileNotFoundError`` when it is
    not installed / not on PATH, or another ``OSError`` when the OS refuses
    to exec it). Subclasses ``BusyThreadError`` on purpose (B856 acceptance
    criterion: "unavailable must NOT dead-letter, must stay pending") -- a
    missing/unconfigured CLI durably parks, it never loses the callback and
    never consumes the transport hard-failure/dead-letter budget.

    A CLI that DID spawn but then timed out, exited non-zero, or returned an
    envelope that does not echo back this exact request is instead a genuine
    transport failure and is raised as ``ClaudeCliProtocolError`` (bounded
    retry / dead-letter) -- see ``ClaudeCliResumeClient._invoke``. Keep the
    two paths distinct: "unavailable" is "cannot even run", not "ran and
    failed"."""


class ClaudeCliProtocolError(ClaudeCliError):
    """The Claude CLI ran and returned SOMETHING, but it was malformed or
    did not genuinely acknowledge (echo back) this exact request -- a real
    transport/protocol failure, so it retains the bounded retry/dead-letter
    path (never silently treated as delivered)."""


class ClaudePanelDeferralError(BusyThreadError):
    """A panel wake transport is configured but did not return a matching
    delivered ack this attempt; durable, non-blocking park like any other
    ``BusyThreadError`` subclass."""


def build_claude_resume_command(
    executable: str | list[str] = "claude", *, resume_session_id: str,
) -> list[str]:
    """Build the ``claude --resume <session_id>`` argv.

    Pure and testable, mirroring ``build_app_server_command``. The exact
    real-world flag surface of the installed ``claude`` CLI is not verified
    against a live binary in this repository, so the invocation shape stays
    fully injectable (``executable`` may be an argv prefix list, and the
    actual subprocess call is itself replaceable via ``ClaudeCliResumeClient``'s
    ``run_fn``) -- correctness is proven by the request/ack round trip in
    tests, never by trusting an unverifiable flag name alone.
    """
    prefix = [executable] if isinstance(executable, str) else list(executable)
    if not prefix or not all(isinstance(item, str) and item for item in prefix):
        raise ValueError("claude executable prefix must be nonempty strings")
    if not resume_session_id or not isinstance(resume_session_id, str):
        raise ValueError("resume_session_id must be a nonempty string")
    return [*prefix, "--resume", resume_session_id, "--print"]


_CLAUDE_CLI_BUSY_MARKERS = ("busy", "active", "already", "in progress", "inprogress")


class ClaudeCliResumeClient:
    """Repository-bound ``claude`` CLI ``--resume <session_id>`` delivery
    transport, parallel in spirit to ``SidebandCallbackClient`` but for a
    one-shot CLI invocation instead of a persistent JSON-RPC socket.

    Sends the same validated callback prompt (``build_callback_prompt``/
    ``build_batch_callback_prompt`` -- only task_id/state/event_id/
    request_id, never worker output) and requires a genuine matching
    acknowledgement (the response envelope must echo back the exact
    ``event_id``/``request_id`` this call sent) before ``delivered=True`` is
    ever reported -- never mark-then-hope.

    ``run_fn`` is injectable (``run_fn(cmd, stdin_payload, timeout) ->
    subprocess.CompletedProcess``-shaped object with ``.returncode``/
    ``.stdout``/``.stderr``) so tests exercise the REAL request/ack code
    path without a real ``claude`` binary. The default ``run_fn`` shells out
    via ``subprocess.run`` and turns a missing executable into
    ``ClaudeCliUnavailableError`` (durable park, never lost) and a timeout
    into ``ClaudeCliProtocolError`` (genuine transport failure).
    """

    def __init__(
        self,
        executable: str | list[str] = "claude",
        *,
        timeout: float = DEFAULT_SIDEBAND_TIMEOUT,
        run_fn: Any | None = None,
    ) -> None:
        self._executable = executable
        self._timeout = timeout
        self._run_fn = run_fn or self._default_run_fn

    @staticmethod
    def _default_run_fn(cmd: list[str], stdin_payload: str, timeout: float) -> Any:
        return subprocess.run(
            cmd, input=stdin_payload, capture_output=True, timeout=timeout,
            text=True, shell=False,
        )

    def _invoke(self, session_id: str, prompt: str) -> dict[str, Any]:
        cmd = build_claude_resume_command(self._executable, resume_session_id=session_id)
        try:
            completed = self._run_fn(cmd, prompt, self._timeout)
        except FileNotFoundError as exc:
            raise ClaudeCliUnavailableError(f"claude cli not found: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCliProtocolError(f"claude cli timed out: {exc}") from exc
        except OSError as exc:
            raise ClaudeCliUnavailableError(f"claude cli unavailable: {exc}") from exc
        returncode = getattr(completed, "returncode", None)
        stdout = getattr(completed, "stdout", "") or ""
        stderr = getattr(completed, "stderr", "") or ""
        combined_lower = f"{stdout}\n{stderr}".lower()
        if any(marker in combined_lower for marker in _CLAUDE_CLI_BUSY_MARKERS):
            raise ClaudeCliBusyError(f"claude session busy: {stderr[:300] or stdout[:300]}")
        if returncode not in (0, None):
            raise ClaudeCliProtocolError(f"claude cli exited {returncode}: {stderr[:300]}")
        try:
            envelope = json.loads(stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ClaudeCliProtocolError(f"claude cli protocol mismatch: {exc}") from exc
        if not isinstance(envelope, dict):
            raise ClaudeCliProtocolError("claude cli protocol mismatch: non-object envelope")
        return envelope

    def deliver_callback(
        self,
        session_id: str,
        task_id: str,
        state: str,
        *,
        event_id: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        prompt = build_callback_prompt(task_id, state, event_id=event_id, request_id=request_id)
        envelope = self._invoke(session_id, prompt)
        if envelope.get("event_id", "") != event_id or envelope.get("request_id", "") != request_id:
            raise ClaudeCliProtocolError("claude cli ack did not match the request (event_id/request_id mismatch)")
        if not envelope.get("ok"):
            raise ClaudeCliProtocolError(f"claude cli reported failure: {envelope.get('error', '')}"[:300])
        return envelope

    def deliver_callback_batch(
        self,
        session_id: str,
        members: list[dict[str, str]],
        *,
        client_user_message_id: str | None = None,
    ) -> dict[str, Any]:
        prompt = build_batch_callback_prompt(members)
        expected_event_id = members[0].get("event_id", "") if len(members) == 1 else ""
        expected_request_id = members[0].get("request_id", "") if len(members) == 1 else ""
        envelope = self._invoke(session_id, prompt)
        if len(members) == 1:
            if (
                envelope.get("event_id", "") != expected_event_id
                or envelope.get("request_id", "") != expected_request_id
            ):
                raise ClaudeCliProtocolError("claude cli ack did not match the request (event_id/request_id mismatch)")
        elif envelope.get("batch_member_count") != len(members):
            raise ClaudeCliProtocolError("claude cli ack did not match the batch (member count mismatch)")
        if not envelope.get("ok"):
            raise ClaudeCliProtocolError(f"claude cli reported failure: {envelope.get('error', '')}"[:300])
        return envelope


class ClaudeCallbackAdapter:
    """Typed Claude callback adapter with persistent session identity.

    Delivery mode is capability-detected, never faked:
    - ``panel``: an explicit ``wake_transport`` callable was injected
      (owned by the VS Code extension side -- out of scope here, B855).
    - ``cli_resume``: no panel transport, but a ``ClaudeCliResumeClient``
      (or compatible object exposing ``deliver_callback_batch``) is
      configured -- delivery is attempted via the repository-bound
      ``claude --resume <session_id>`` CLI transport, and ``delivered`` is
      only ever set True after a genuine matching acknowledgement.
    - ``unavailable``: neither is configured -- a durable, honestly-labeled
      pending/resumable state, never a fabricated delivery.
    """

    provider = "claude"

    def __init__(
        self,
        *,
        wake_transport: Any | None = None,
        cli_client: Any | None = None,
    ) -> None:
        self._wake_transport = wake_transport
        self._cli_client = cli_client

    @property
    def mode(self) -> str:
        if self._wake_transport is not None:
            return "panel"
        if self._cli_client is not None:
            return "cli_resume"
        return "unavailable"

    def deliver_or_defer(
        self,
        route: CoordinatorRouteKey,
        *,
        payload: Mapping[str, Any] | None = None,
        raise_on_defer: bool = False,
    ) -> CallbackDeliveryResult:
        if route.provider != self.provider:
            raise ValueError("claude adapter received non-claude route")
        if not route.session_id:
            raise ValueError("claude adapter requires persistent session_id")
        payload = dict(payload or {})
        mode = self.mode

        if mode == "unavailable":
            reason = "live_claude_panel_wake_transport_unavailable"
            if raise_on_defer:
                raise ClaudeCliUnavailableError(reason)
            return CallbackDeliveryResult(
                provider=self.provider,
                route=route,
                state="deferred_resumable",
                durable=True,
                delivered=False,
                action="resume_claude_session",
                reason=reason,
            )

        if mode == "panel":
            result = self._wake_transport(route=route, payload=payload)
            ack_matches = bool(
                isinstance(result, Mapping)
                and result.get("delivered") is True
                and result.get("event_id", "") == payload.get("event_id", "")
                and result.get("request_id", "") == payload.get("request_id", "")
            )
            if not ack_matches and raise_on_defer:
                raise ClaudePanelDeferralError("claude_panel_wake_deferred_or_unmatched_ack")
            return CallbackDeliveryResult(
                provider=self.provider,
                route=route,
                state="delivered" if ack_matches else "deferred_resumable",
                durable=not ack_matches,
                delivered=ack_matches,
                action="delivered" if ack_matches else "resume_claude_session",
                reason="" if ack_matches else "claude_panel_wake_deferred",
            )

        # mode == "cli_resume"
        members = payload.get("members") or [{
            "task_id": route.task_id,
            "state": payload.get("state", ""),
            "event_id": payload.get("event_id", ""),
            "request_id": payload.get("request_id", ""),
        }]
        try:
            self._cli_client.deliver_callback_batch(
                route.session_id, members,
                client_user_message_id=payload.get("client_user_message_id"),
            )
        except ClaudeCliBusyError as exc:
            if raise_on_defer:
                raise
            return CallbackDeliveryResult(
                provider=self.provider, route=route, state="deferred_resumable",
                durable=True, delivered=False, action="resume_claude_session",
                reason=f"claude_cli_busy:{exc}"[:500],
            )
        except ClaudeCliUnavailableError as exc:
            if raise_on_defer:
                raise
            return CallbackDeliveryResult(
                provider=self.provider, route=route, state="deferred_resumable",
                durable=True, delivered=False, action="resume_claude_session",
                reason=f"claude_cli_resume_unavailable:{exc}"[:500],
            )
        return CallbackDeliveryResult(
            provider=self.provider, route=route, state="delivered",
            durable=False, delivered=True, action="delivered", reason="",
        )


def _select_newest_inprogress_turn_id(in_progress: list[dict[str, Any]]) -> str | None:
    """Return the uniquely newest credible in-progress turn by ``startedAt``.

    ``thread/resume`` includes historical turns.  Some older Codex sessions
    retain their historical status as ``inProgress``; choosing the first or
    last list entry would therefore be unsafe.  A unique maximum timestamp is
    the only deterministic candidate, and ``turn/steer`` remains the server-
    side authority that accepts or rejects it.
    """
    candidates: list[tuple[int, str]] = []
    for turn in in_progress:
        turn_id = turn.get("id")
        started_at = turn.get("startedAt")
        if not turn_id or isinstance(started_at, bool) or not isinstance(started_at, int):
            continue
        candidates.append((started_at, str(turn_id)))
    if not candidates:
        return None
    newest_started_at = max(started_at for started_at, _ in candidates)
    newest_ids = {turn_id for started_at, turn_id in candidates if started_at == newest_started_at}
    if len(newest_ids) != 1:
        return None
    return next(iter(newest_ids))


def select_steer_target(resume_response: dict[str, Any]) -> str | None:
    """Decide the delivery path from a successful ``thread/resume`` response.

    Per the installed App Server's ``ThreadResumeResponse``/``Thread``/
    ``ThreadStatus``/``Turn`` schema: ``result.thread.status`` is a
    discriminated union (``{"type": "idle"}``, ``{"type": "active",
    "activeFlags": [...]}``, ``{"type": "notLoaded"}``, ``{"type":
    "systemError"}``) and ``result.thread.turns`` is fully populated on
    ``thread/resume`` (each turn's ``status`` one of ``completed`` /
    ``interrupted`` / ``failed`` / ``inProgress``).

    Returns ``None`` when idle -- the existing ``turn/start`` path.
    Returns the single in-progress turn's id when active with exactly one
    ``inProgress`` turn.  For multiple historical ``inProgress`` rows, returns
    the uniquely newest one by non-null ``startedAt``.  Ambiguous/missing
    recency still defers and never starts a second turn.
    """
    result = resume_response.get("result")
    thread = result.get("thread") if isinstance(result, dict) else None
    if not isinstance(thread, dict):
        raise AppServerError("thread/resume response missing result.thread")
    status = thread.get("status")
    status_type = status.get("type") if isinstance(status, dict) else None
    if status_type == "idle":
        return None
    if status_type != "active":
        raise ActiveThreadSteerDeferralError(f"thread_status_not_steerable:{status_type}")
    turns = thread.get("turns")
    if not isinstance(turns, list):
        raise ActiveThreadSteerDeferralError("thread_turns_missing_or_malformed")
    in_progress = [t for t in turns if isinstance(t, dict) and t.get("status") == "inProgress"]
    if not in_progress:
        raise ActiveThreadSteerDeferralError("inprogress_turn_count:0")
    if len(in_progress) == 1:
        turn_id = in_progress[0].get("id")
        if not turn_id:
            raise ActiveThreadSteerDeferralError("inprogress_turn_missing_id")
        return str(turn_id)
    newest_turn_id = _select_newest_inprogress_turn_id(in_progress)
    if newest_turn_id is None:
        raise ActiveThreadSteerDeferralError(f"inprogress_turn_count:{len(in_progress)}")
    return newest_turn_id


# --- B409: extension-owned App Server sideband transport --------------------

class SidebandUnavailableError(AppServerError):
    """The local extension-owned App Server sideband (``app_server_mux.py``)
    could not be reached, is not yet ready, or the response did not match
    the expected wire shape (protocol mismatch). This is a genuine
    transport/protocol failure and therefore consumes the bounded hard
    failure budget. Never triggers a fallback separate App Server spawn."""


class SidebandThreadBusyError(BusyThreadError):
    """The sideband is healthy but the exact destination thread rejected
    resume/start because it is busy. This is a durable busy park, not a
    sideband transport failure."""


class SidebandNotReadyError(BusyThreadError):
    """The resolved mux owner reported ``not_ready``.

    This means its handshake/child App Server is still starting or busy with
    another turn.  It is a transient thread-readiness condition, so callback
    delivery must use the durable busy-park path instead of consuming the
    bounded transport-failure/dead-letter budget.
    """


class SidebandRejectedError(AppServerError):
    """The sideband rejected this request (bad capability, disallowed
    method, malformed shape) -- a hard, non-retryable protocol error."""


class SidebandOwnerResolutionError(BusyThreadError):
    """B472: missing, stale, or ambiguous mux-instance ownership for the
    origin thread -- a durable, non-blocking park, exactly like an
    ordinary busy/active thread. Never a hard transport failure and never
    a guess: this client addresses exactly one uniquely-resolved live
    owner instance or it does not send at all."""


class SidebandOwnerNotFoundError(SidebandOwnerResolutionError):
    """No live mux instance has observed the extension's OWN traffic
    (thread/resume / turn/start / turn/steer) binding this thread yet, or
    its only prior owner has since exited. A sideband-issued resume probe
    from this very client never counts as ownership traffic, so this is
    the expected state until the extension itself touches the thread."""


class SidebandOwnerAmbiguousError(SidebandOwnerResolutionError):
    """More than one live mux instance's own observed extension traffic
    claims this thread. Never guessed between -- parked until the next
    retry finds the ambiguity resolved (or still ambiguous, parking again)."""


_SIDEBAND_TURN_START_BUSY_MARKERS = (
    "busy",
    "active",
    "already",
    "in progress",
    "inprogress",
    "not_ready",
    "not ready",
    "steerable",
)


def _is_sideband_turn_start_busy_rejection(error: Any) -> bool:
    """Return whether a sideband ``turn/start`` rejection means busy.

    Both its free-text message and the structured
    ``CodexErrorInfo.activeTurnNotSteerable`` shape are recognized. These
    conditions durably park the callback instead of consuming its hard-failure
    budget.
    """
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict):
            codex_error_info = data.get("codexErrorInfo")
            if isinstance(codex_error_info, dict) and "activeTurnNotSteerable" in codex_error_info:
                return True
        message = str(error.get("message", ""))
    else:
        message = str(error)
    lowered = message.lower()
    return any(marker in lowered for marker in _SIDEBAND_TURN_START_BUSY_MARKERS)


class SidebandCallbackClient:
    """Callback transport that reaches the extension-owned App Server
    through ``app_server_mux.py``'s authenticated local Unix sideband
    socket instead of spawning a separate ``codex app-server`` subprocess
    (B409) -- closing the exact defect ``AppServerClient`` cannot: a
    separately spawned App Server instance can never observe, let alone
    wake, the thread the VS Code extension's OWN App Server child owns.

    B472: several VS Code windows may run concurrent mux instances against
    the same ``sideband_dir``. Before every call this client resolves the
    SINGLE live mux instance whose own observed extension traffic bound
    ``thread_id`` (``find_owning_sideband_instances``) and addresses only
    that instance's socket/capability pair -- never the newest, never a
    fixed shared path, never a fan-out to more than one candidate. Missing
    or ambiguous ownership raises a ``BusyThreadError`` subclass so the
    caller durably parks the whole batch exactly like an ordinary busy
    thread, never consuming the hard-failure budget.

    B684 delivery sends one direct ``turn/start`` and never performs a
    preceding ``thread/resume`` or a ``turn/steer`` fallback. The App
    Server's synchronous response is the atomic concurrency decision: a valid
    ``TurnStartResponse.turn.id`` acknowledges delivery, while an active/busy
    rejection durably parks the batch. This avoids loading a very large thread
    history merely to determine whether it is idle.
    """

    def __init__(
        self,
        *,
        repo_id: str,
        sideband_dir: Path | str | None = None,
        timeout: float = DEFAULT_SIDEBAND_TIMEOUT,
    ) -> None:
        # B925: repo_id is mandatory -- this transport must never resolve
        # ownership without knowing exactly which repository it is bound
        # to, closing the exact cross-repository leakage this client
        # exists to prevent (a Secp256K1fast thread resolving into a
        # GeoAI mux instance, or vice versa).
        self._repo_id = _validate_sideband_repo_id(repo_id)
        self._sideband_dir = Path(sideband_dir) if sideband_dir else default_sideband_dir()
        self._timeout = timeout
        self._socket_lock = threading.Lock()
        self._active_socket: _socket.socket | None = None

    def stop(self) -> None:
        """Interrupt an in-flight sideband read during extension shutdown.

        Without this, reload killed the dispatcher process while its durable
        batch remained leased for the full App Server timeout, blocking the
        replacement dispatcher for the same coordinator thread.
        """
        with self._socket_lock:
            sock = self._active_socket
            self._active_socket = None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(_socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()

    def _resolve_owner(self, thread_id: str):
        instances = find_owning_sideband_instances(self._sideband_dir, thread_id, self._repo_id)
        if not instances:
            raise SidebandOwnerNotFoundError("no live mux instance owns this thread")
        if len(instances) > 1:
            raise SidebandOwnerAmbiguousError(
                f"{len(instances)} live mux instances claim this thread"
            )
        return instances[0]

    def _read_capability(self, capability_path: Path) -> str:
        try:
            return capability_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SidebandUnavailableError(f"sideband capability unavailable: {exc}") from exc

    def _recv_line(self, sock: _socket.socket) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = sock.recv(4096)
            except OSError as exc:
                raise SidebandUnavailableError(f"sideband read failed: {exc}") from exc
            if not chunk:
                break
            total += len(chunk)
            if total > SIDEBAND_RESPONSE_MAX_BYTES:
                raise SidebandUnavailableError("sideband response exceeded bounded size")
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        return b"".join(chunks)

    def _call(self, method: str, params: dict[str, Any], *, thread_id: str) -> dict[str, Any]:
        owner = self._resolve_owner(thread_id)
        token = self._read_capability(owner.capability_path)
        try:
            sock = connect_sideband_socket(owner.socket_path, timeout=self._timeout)
        except OSError as exc:
            raise SidebandUnavailableError(f"sideband socket unreachable: {exc}") from exc
        with self._socket_lock:
            self._active_socket = sock
        try:
            payload = json.dumps({"cap": token, "method": method, "params": params}, ensure_ascii=False)
            try:
                sock.sendall((payload + "\n").encode("utf-8"))
                sock.shutdown(_socket.SHUT_WR)
            except OSError as exc:
                raise SidebandUnavailableError(f"sideband write failed: {exc}") from exc
            raw = self._recv_line(sock)
        finally:
            with self._socket_lock:
                if self._active_socket is sock:
                    self._active_socket = None
            with contextlib.suppress(OSError):
                sock.close()

        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SidebandUnavailableError(f"sideband protocol mismatch: {exc}") from exc
        if not isinstance(envelope, dict):
            raise SidebandUnavailableError("sideband protocol mismatch: non-object envelope")
        if not envelope.get("ok"):
            error = str(envelope.get("error", "sideband_error"))
            if error == "not_ready":
                raise SidebandNotReadyError(error)
            raise SidebandRejectedError(error)
        response = envelope.get("response")
        if not isinstance(response, dict):
            raise SidebandUnavailableError("sideband protocol mismatch: missing response object")
        return response

    def thread_resume(self, thread_id: str, *, cwd: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id}
        if cwd:
            params["cwd"] = cwd
        response = self._call("thread/resume", params, thread_id=thread_id)
        if "error" in response:
            err = response["error"]
            message = str(err.get("message", "") if isinstance(err, dict) else err)
            if "busy" in message.lower():
                raise SidebandThreadBusyError("thread_busy")
            raise AppServerError(f"thread/resume failed: {err}")
        if "result" not in response:
            raise AppServerError(f"thread/resume returned no result: {response}")
        return response

    def turn_steer(
        self,
        thread_id: str,
        prompt: str,
        *,
        expected_turn_id: str,
        client_user_message_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "expectedTurnId": expected_turn_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if client_user_message_id:
            params["clientUserMessageId"] = client_user_message_id
        response = self._call("turn/steer", params, thread_id=thread_id)
        if "error" in response:
            raise ActiveThreadSteerDeferralError(f"turn_steer_rejected: {response['error']}"[:300])
        result = response.get("result")
        turn_id = result.get("turnId") if isinstance(result, dict) else None
        if not turn_id:
            raise AppServerError("sideband turn/steer response missing result.turnId")
        if turn_id != expected_turn_id:
            raise AppServerError("sideband turn/steer response turnId mismatch")
        return response

    def turn_start(
        self,
        thread_id: str,
        prompt: str,
        *,
        cwd: str | None = None,
        client_user_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Deliver via the synchronous ``turn/start`` acknowledgement alone.

        Active/busy/activeTurnNotSteerable rejections are durable busy parks;
        malformed, denied, and protocol-shaped errors retain the bounded
        hard-failure path.
        """
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if cwd:
            params["cwd"] = cwd
        if client_user_message_id:
            params["clientUserMessageId"] = client_user_message_id
        response = self._call("turn/start", params, thread_id=thread_id)
        if "error" in response:
            err = response["error"]
            if _is_sideband_turn_start_busy_rejection(err):
                raise SidebandThreadBusyError(f"turn_start_busy_rejection: {err}"[:300])
            raise AppServerError(f"turn/start failed: {err}")
        result = response.get("result")
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not turn_id:
            raise AppServerError("sideband turn/start response missing result.turn.id")
        return response

    def _deliver_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        cwd: str,
        client_user_message_id: str | None,
    ) -> dict[str, Any]:
        """Send one direct ``turn/start`` without loading thread history."""
        return self.turn_start(thread_id, prompt, cwd=cwd, client_user_message_id=client_user_message_id)

    def deliver_callback(
        self,
        thread_id: str,
        task_id: str,
        state: str,
        *,
        event_id: str = "",
        request_id: str = "",
        client_user_message_id: str | None = None,
        cwd: str = CALLBACK_CWD,
    ) -> dict[str, Any]:
        prompt = build_callback_prompt(task_id, state, event_id=event_id, request_id=request_id)
        return self._deliver_turn(thread_id, prompt, cwd=cwd, client_user_message_id=client_user_message_id)

    def deliver_callback_batch(
        self,
        thread_id: str,
        members: list[dict[str, str]],
        *,
        client_user_message_id: str | None = None,
        cwd: str = CALLBACK_CWD,
    ) -> dict[str, Any]:
        prompt = build_batch_callback_prompt(members)
        return self._deliver_turn(thread_id, prompt, cwd=cwd, client_user_message_id=client_user_message_id)


# --- App Server protocol client ---------------------------------------------

class AppServerClient:
    """Newline-delimited JSON client speaking the real Codex App Server wire.

    Lifecycle: initialize -> initialized -> thread/resume -> (idle:
    turn/start -> wait for turn/completed) or (active with exactly one
    inProgress turn: turn/steer -> matching TurnSteerResponse.turnId).
    Messages carry NO top-level ``jsonrpc`` member (the installed server's
    own JSON Schema requires only id/method/params or id/result or
    id/error -- adding an extra field the schema does not declare risks
    strict-deserialization rejection on the real binary).
    """

    def __init__(
        self,
        executable: str | list[str] = "codex",
        *,
        repo: Path | str | None = None,
        timeout: float = DEFAULT_APP_SERVER_TIMEOUT,
    ):
        self._executable = executable
        self._repo = Path(repo) if repo else Path.cwd()
        self._timeout = timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._stdout_queue: queue.Queue[bytes | None] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._next_request_id = 0
        self._initialized = False

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _new_id(self) -> int:
        self._next_request_id += 1
        return self._next_request_id

    def _send(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.poll() is not None:
            raise AppServerError("app server process is not running")
        payload = json.dumps(message, ensure_ascii=False)
        try:
            self._process.stdin.write((payload + "\n").encode("utf-8"))  # type: ignore[union-attr]
            self._process.stdin.flush()  # type: ignore[union-attr]
        except (BrokenPipeError, OSError) as exc:
            raise AppServerError(f"failed to write to app server: {exc}") from exc

    def _recv_raw(self, deadline: float) -> dict[str, Any]:
        if self._process is None:
            raise AppServerError("app server process is not running")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError("timed out waiting for app server response")
            stdout = self._process.stdout
            if stdout is None:
                raise AppServerError("app server stdout pipe is unavailable")
            if os.name == "nt":
                if self._stdout_queue is None:
                    raise AppServerError("app server stdout reader is unavailable")
                try:
                    line = self._stdout_queue.get(timeout=min(remaining, 0.1))
                except queue.Empty:
                    if self._process.poll() is not None:
                        raise AppServerError(
                            f"app server exited with code {self._process.returncode}"
                        )
                    continue
                if line is None:
                    raise AppServerError(
                        f"app server exited with code {self._process.poll()}"
                    )
            else:
                if self._process.poll() is not None:
                    raise AppServerError(
                        f"app server exited with code {self._process.returncode}"
                    )
                try:
                    readable, _, _ = select.select([stdout], [], [], min(remaining, 0.1))
                except (OSError, ValueError) as exc:
                    raise AppServerError(f"app server stdout wait failed: {exc}") from exc
                if not readable:
                    continue
                line = stdout.readline()
            if not line:
                time.sleep(0.01)
                continue
            try:
                decoded = json.loads(line.decode("utf-8").strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(decoded, dict):
                return decoded

    def _read_windows_stdout(self, process: subprocess.Popen[bytes]) -> None:
        output = self._stdout_queue
        stdout = process.stdout
        if output is None or stdout is None:
            return
        try:
            while True:
                line = stdout.readline()
                if not line:
                    break
                output.put(line)
        except (OSError, ValueError):
            pass
        finally:
            output.put(None)

    def _recv_response_for(self, request_id: int, *, timeout: float | None = None) -> dict[str, Any]:
        """Read messages until the response matching ``request_id`` arrives.

        Server-pushed notifications (``session/*``, ``turn/started``, etc.)
        that may interleave before the synchronous response are tolerated
        and skipped.
        """
        deadline = time.monotonic() + (timeout if timeout is not None else self._timeout)
        while True:
            msg = self._recv_raw(deadline)
            if "method" in msg and "id" not in msg:
                continue  # unrelated notification; keep waiting
            if msg.get("id") == request_id:
                return msg
            # A response to a different id should not happen on this
            # single-threaded bridge; ignore rather than misattribute.

    def start(self) -> None:
        if self._process is not None:
            raise AppServerError("app server already started")
        cmd = build_app_server_command(self._executable)
        for flag in _REJECTED_FLAGS:
            if flag in cmd:
                raise AppServerError(f"rejected flag {flag} in app server command")
        if "app-server" not in cmd:
            raise AppServerError("app server command missing required app-server subcommand")
        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=str(self._repo),
                shell=False,
                start_new_session=True,
                bufsize=0,
            )
            if os.name == "nt":
                self._stdout_queue = queue.Queue()
                self._stdout_thread = threading.Thread(
                    target=self._read_windows_stdout,
                    args=(self._process,),
                    daemon=True,
                )
                self._stdout_thread.start()
            self._initialized = False
        except (OSError, ValueError) as exc:
            raise AppServerError(f"failed to start app server: {exc}") from exc

    def stop(self) -> None:
        if self._process is None:
            return
        try:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            reader = self._stdout_thread
            if reader is not None:
                reader.join(timeout=1)
            self._stdout_thread = None
            self._stdout_queue = None
            self._process = None
            self._initialized = False

    def initialize(self) -> dict[str, Any]:
        """Send ``initialize`` and wait for its successful response.

        ``InitializeParams`` requires only ``clientInfo`` (itself requiring
        ``name``+``version``); ``capabilities`` is optional.
        """
        req_id = self._new_id()
        self._send({
            "id": req_id,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "aiworkhub-callback-bridge",
                    "version": "1.0.0",
                },
                "capabilities": {},
            },
        })
        response = self._recv_response_for(req_id)
        if "error" in response:
            raise AppServerError(f"initialize failed: {response['error']}")
        if "result" not in response:
            raise AppServerError(f"initialize returned no result: {response}")
        return response

    def send_initialized(self) -> None:
        """Send the ``initialized`` notification (method only, no params key)."""
        self._send({"method": "initialized"})

    def thread_resume(self, thread_id: str, *, cwd: str | None = None) -> dict[str, Any]:
        """Resume the bound thread. Raises ``BusyThreadError`` if busy."""
        req_id = self._new_id()
        params: dict[str, Any] = {"threadId": thread_id}
        if cwd:
            params["cwd"] = cwd
        self._send({"id": req_id, "method": "thread/resume", "params": params})
        response = self._recv_response_for(req_id)
        if "error" in response:
            err = response["error"]
            message = str(err.get("message", "") if isinstance(err, dict) else err)
            if "busy" in message.lower():
                raise BusyThreadError("thread_busy")
            raise AppServerError(f"thread/resume failed: {err}")
        if "result" not in response:
            raise AppServerError(f"thread/resume returned no result: {response}")
        result = response.get("result")
        returned_thread = result.get("thread") if isinstance(result, dict) else None
        returned_thread_id = returned_thread.get("id") if isinstance(returned_thread, dict) else None
        if returned_thread_id != thread_id:
            raise AppServerError("thread/resume response thread identity mismatch")
        return response

    def turn_start(
        self,
        thread_id: str,
        prompt: str,
        *,
        cwd: str | None = None,
        client_user_message_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Start a callback turn and return on the matching acceptance ACK.

        ``TurnStartParams`` requires ``threadId`` and ``input`` (an array of
        ``UserInput``); ``cwd``/``clientUserMessageId`` are optional. The
        synchronous response contains ``result.turn.id`` and is the transport
        acknowledgement: at that point the callback message has already been
        accepted by the App Server.  The later assistant-turn outcome is not a
        delivery verdict.  A cancelled/interrupted/failed ``turn/completed``
        must therefore never cause the already-injected callback to be retried
        (which would duplicate the user-visible wake and eventually
        dead-letter it).  Retry remains limited to failures before this exact
        request acknowledgement.
        """
        req_id = self._new_id()
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if cwd:
            params["cwd"] = cwd
        if client_user_message_id:
            params["clientUserMessageId"] = client_user_message_id
        self._send({"id": req_id, "method": "turn/start", "params": params})

        deadline = time.monotonic() + (timeout if timeout is not None else self._timeout)
        while True:
            msg = self._recv_raw(deadline)
            if msg.get("id") == req_id:
                if "error" in msg:
                    err = msg["error"]
                    message = str(err.get("message", "") if isinstance(err, dict) else err)
                    if "busy" in message.lower():
                        raise BusyThreadError("thread_busy")
                    raise AppServerError(f"turn/start failed: {err}")
                result = msg.get("result")
                turn = result.get("turn") if isinstance(result, dict) else None
                acknowledged_turn_id = turn.get("id") if isinstance(turn, dict) else None
                if not acknowledged_turn_id:
                    raise AppServerError("turn/start response missing result.turn.id")
                return msg
            # Ignore other interleaved notifications (turn/started,
            # turn/completed, turn/diff/updated, turn/plan/updated, etc.) while
            # waiting for the exact synchronous request acknowledgement.
        # (unreachable: loop returns or raises via timeout inside _recv_raw)

    def turn_steer(
        self,
        thread_id: str,
        prompt: str,
        *,
        expected_turn_id: str,
        client_user_message_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Append ``prompt`` to the already in-progress turn via ``turn/steer``.

        ``TurnSteerParams`` requires ``threadId``, ``expectedTurnId``, and
        ``input`` (verified against the installed binary's own
        ``TurnSteerParams``/``TurnSteerResponse`` JSON Schema export --
        ``TurnSteerResponse`` requires only ``turnId``). A matching
        successful ``TurnSteerResponse.turnId`` IS the full delivery
        acknowledgement: steering appends to the existing in-flight turn,
        so this call returns as soon as that response arrives and never
        waits for a later ``turn/completed`` notification. Any error
        response -- a stale ``expectedTurnId`` (the request fails
        server-side once it no longer matches the currently active turn)
        or an ``activeTurnNotSteerable`` review/compact turn -- is a
        bounded deferral (``ActiveThreadSteerDeferralError``), never
        retried as a second ``turn/start``.
        """
        req_id = self._new_id()
        params: dict[str, Any] = {
            "threadId": thread_id,
            "expectedTurnId": expected_turn_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if client_user_message_id:
            params["clientUserMessageId"] = client_user_message_id
        self._send({"id": req_id, "method": "turn/steer", "params": params})
        response = self._recv_response_for(req_id, timeout=timeout)
        if "error" in response:
            err = response["error"]
            message = str(err.get("message", "") if isinstance(err, dict) else err)
            raise ActiveThreadSteerDeferralError(f"turn_steer_rejected: {message}"[:300])
        result = response.get("result")
        turn_id = result.get("turnId") if isinstance(result, dict) else None
        if not turn_id:
            raise AppServerError("turn/steer response missing result.turnId")
        if turn_id != expected_turn_id:
            raise AppServerError("turn/steer response turnId mismatch")
        return response

    def _deliver_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        cwd: str,
        client_user_message_id: str | None,
    ) -> dict[str, Any]:
        """Resume the thread, then deliver via the correct path.

        Idle -> ``turn/start`` and wait for ``turn/completed`` (unchanged
        B384/B402 behavior). Active with exactly one steerable
        ``inProgress`` turn -> ``turn/steer`` onto that exact turn,
        acknowledged by ``TurnSteerResponse.turnId`` alone. Every other
        active-thread shape defers (``ActiveThreadSteerDeferralError``)
        -- ``turn/start`` is never called on an already-active thread.
        """
        resume_response = self.thread_resume(thread_id)
        expected_turn_id = select_steer_target(resume_response)
        if expected_turn_id is None:
            return self.turn_start(
                thread_id,
                prompt,
                cwd=cwd,
                client_user_message_id=client_user_message_id,
            )
        return self.turn_steer(
            thread_id,
            prompt,
            expected_turn_id=expected_turn_id,
            client_user_message_id=client_user_message_id,
        )

    def deliver_callback(
        self,
        thread_id: str,
        task_id: str,
        state: str,
        *,
        event_id: str = "",
        request_id: str = "",
        client_user_message_id: str | None = None,
        cwd: str = CALLBACK_CWD,
    ) -> dict[str, Any]:
        """Full delivery: initialize, resume thread, steer-or-start, ack."""
        if not self._initialized:
            self.initialize()
            self.send_initialized()
            self._initialized = True
        prompt = build_callback_prompt(task_id, state, event_id=event_id, request_id=request_id)
        return self._deliver_turn(
            thread_id, prompt, cwd=cwd, client_user_message_id=client_user_message_id,
        )

    def deliver_callback_batch(
        self,
        thread_id: str,
        members: list[dict[str, str]],
        *,
        client_user_message_id: str | None = None,
        cwd: str = CALLBACK_CWD,
    ) -> dict[str, Any]:
        """Full batch delivery: one turn (started or steered) covering every member.

        Coalesces every member sharing ``thread_id`` into a single
        ``turn/start``+``turn/completed`` round trip when idle, or a
        single ``turn/steer`` append when the thread already has one
        in-progress turn -- never one turn per task (B402: eight
        near-simultaneous review_ready events previously produced up to
        eight separate turns/dead letters) and never a second turn
        started alongside an already-active one (B407)."""
        if not self._initialized:
            self.initialize()
            self.send_initialized()
            self._initialized = True
        prompt = build_batch_callback_prompt(members)
        return self._deliver_turn(
            thread_id, prompt, cwd=cwd, client_user_message_id=client_user_message_id,
        )


# --- Outbox entry (mirrors callback_store.py::callback_outbox row shape) ---

@dataclass
class CallbackEntry:
    outbox_id: int
    task_id: str
    origin_thread_id: str
    transition: str
    episode_id: str
    event_id: str
    request_id: str
    state: str
    attempts: int
    lease_id: str
    lease_expires_at: str
    last_error: str = ""

    @property
    def dedup_key(self) -> str:
        return f"{self.task_id}:{self.transition}:{self.origin_thread_id}:{self.episode_id}"


@dataclass
class CallbackBatch:
    """One coalesced delivery batch: durable identity/lease from
    ``callback_batches`` plus its member ``CallbackEntry`` rows, all
    sharing the same ``origin_thread_id`` (a batch never spans threads)."""

    batch_id: str
    origin_thread_id: str
    lease_id: str
    lease_expires_at: str
    attempts: int
    members: list[CallbackEntry]

    @property
    def size(self) -> int:
        return len(self.members)

    def as_prompt_members(self) -> list[dict[str, str]]:
        return [
            {
                "task_id": m.task_id,
                "state": m.transition,
                "event_id": m.event_id,
                "request_id": m.request_id,
            }
            for m in self.members
        ]


def _entry_from_row(row: dict[str, Any]) -> CallbackEntry:
    return CallbackEntry(
        outbox_id=int(row["outbox_id"]),
        task_id=str(row["task_id"]),
        origin_thread_id=str(row["origin_thread_id"]),
        transition=str(row["transition"]),
        episode_id=str(row.get("episode_id") or "0"),
        event_id=str(row.get("event_id") or ""),
        request_id=str(row.get("request_id") or ""),
        state=str(row.get("state") or "pending"),
        attempts=int(row.get("attempts") or 0),
        lease_id=str(row.get("lease_id") or ""),
        lease_expires_at=str(row.get("lease_expires_at") or ""),
        last_error=str(row.get("last_error") or ""),
    )


def _batch_from_claim_result(claimed: dict[str, Any]) -> CallbackBatch:
    return CallbackBatch(
        batch_id=str(claimed["batch_id"]),
        origin_thread_id=str(claimed["origin_thread_id"]),
        lease_id=str(claimed["lease_id"]),
        lease_expires_at=str(claimed["lease_expires_at"]),
        attempts=int(claimed["attempts"]),
        members=[_entry_from_row(row) for row in claimed["members"]],
    )


# --- Bridge runner ------------------------------------------------------

class CallbackBridge:
    """Top-level bridge: durable-outbox consumer + App Server client.

    The durable outbox lives in the package-local ``callback_store.py``
    module on the SAME canonical, repo-local
    ``.aiworkhub/tasking/task_queue.sqlite`` that ``taskctl.py``/
    ``AITools/taskdb.py`` write to when that repository has an AITools
    install -- this class only claims/delivers/finalizes entries through
    ``callback_store.py``'s primitives, which share that exact table
    schema, so the bridge and taskctl never disagree about outbox state.
    A packaged VSIX runtime with no ``AITools`` directory anywhere on
    ``sys.path`` still has full callback delivery.
    """

    def __init__(
        self,
        *,
        repo: Path | str | None = None,
        db_path: Path | str | None = None,
        state_path: Path | str | None = None,
        executable: str | list[str] = "codex",
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        app_server_timeout: float = DEFAULT_APP_SERVER_TIMEOUT,
        callback_cwd: str = CALLBACK_CWD,
        max_batch_members: int | None = None,
        lease_margin_seconds: float = DEFAULT_LEASE_MARGIN_SECONDS,
        transport: str = "subprocess",
        sideband_dir: Path | str | None = None,
        sideband_repo_id: str = "",
        claude_executable: str | list[str] = "claude",
        claude_repo_id: str = "",
        claude_window_id: str = "",
        claude_cli_run_fn: Any | None = None,
        provider: str = "",
    ):
        # lease_margin_seconds is a deliberate, visible constructor override
        # for tests/dev exercising short lease-expiry scenarios; the CLI
        # (main()/resolve_bridge_settings()) never exposes it, so the
        # production safety margin is never operator-configurable away.
        validate_lease_and_timeout(lease_seconds, app_server_timeout, margin_seconds=lease_margin_seconds)
        if transport not in ("subprocess", "sideband", "claude_cli"):
            raise ValueError(f"unknown callback transport: {transport}")
        if transport == "claude_cli" and not (claude_repo_id and claude_window_id):
            # Fail closed at construction: a claude_cli bridge instance must
            # know its exact repo/window identity up front -- no cross-
            # repository fallback and no guessed identity (B856).
            raise ValueError(
                "claude_cli transport requires claude_repo_id and claude_window_id"
            )
        if transport == "sideband" and not sideband_repo_id:
            # Fail closed at construction, mirroring the claude_cli case
            # above: a sideband bridge instance must know its exact
            # repository identity up front so ownership resolution can
            # never fall back to a shared/global default (B925).
            raise ValueError("sideband transport requires sideband_repo_id")
        self._callback_store = callback_store
        self._repo = Path(repo).expanduser().resolve() if repo else _bound_repo_from_env_or_cwd()
        self._db_path = Path(db_path) if db_path else self._callback_store.resolve_db_path(self._repo)
        self._state_path = Path(state_path) if state_path else (
            self._repo / "tools" / "aiworkhub" / "logs" / "callback_bridge_state.json"
        )
        self._executable = executable
        self._lease_seconds = lease_seconds
        self._app_server_timeout = app_server_timeout
        self._callback_cwd = callback_cwd
        self._max_batch_members = int(max_batch_members) if max_batch_members else int(
            self._callback_store.DEFAULT_CALLBACK_BATCH_MAX_MEMBERS
        )
        # "subprocess" (default, unchanged B384/B402/B407 behavior) spawns
        # its own AppServerClient over a fresh `codex app-server` child --
        # correct only while no extension owns the thread already.
        # "sideband" (B409) instead reaches the extension-owned App Server
        # through app_server_mux.py's local socket; Codex wires this in as
        # the live default only after the VS Code setting + canary land.
        # "claude_cli" (B856) delivers via a repository-bound
        # ``claude --resume <session_id>`` CLI transport instead of Codex's
        # App Server wire protocol. The durable outbox schema in
        # ``callback_store.py`` now carries a ``provider`` column on both
        # ``callback_outbox`` and ``callback_batches`` and
        # ``claim_pending_callback_batch`` filters on it, so provider
        # isolation is enforced at the store level. A claude_cli-transport
        # bridge instance additionally binds its ENTIRE queue as
        # claude-routed -- the existing "route through the
        # origin_thread_id-keyed rows using the persistent session_id in
        # that slot" pattern -- and is still deployed as a separate bridge
        # process/db from any codex-transport bridge for the same repo,
        # never mixed in one instance.
        self._transport = transport
        self._provider = str(provider or "").strip().lower()
        self._sideband_dir = Path(sideband_dir) if sideband_dir else default_sideband_dir()
        self._sideband_repo_id = str(sideband_repo_id or "")
        self._claude_executable = claude_executable
        self._claude_repo_id = claude_repo_id
        self._claude_window_id = claude_window_id
        self._claude_cli_run_fn = claude_cli_run_fn
        self._claude_adapter: ClaudeCallbackAdapter | None = None
        self._client: AppServerClient | None = None
        self._sideband_client: SidebandCallbackClient | None = None
        self._stop_event = threading.Event()
        self._startup_recovered_batch_count = 0

    def _conn(self):
        conn = self._callback_store.open_db(self._db_path)
        self._callback_store.init_db(conn)
        return conn

    def recover_incompatible_transport_leases(self) -> int:
        """Startup-only transport migration; direct bridge construction
        remains side-effect free until the dispatcher actually starts it."""
        if self._transport != "sideband":
            return 0
        conn = self._conn()
        try:
            recovered = self._callback_store.recover_incompatible_long_inflight_batches(
                conn,
                provider=self._provider or "codex",
                max_lease_span_seconds=float(SIDEBAND_LEASE_SECONDS),
            )
        finally:
            conn.close()
        self._startup_recovered_batch_count += recovered
        return recovered

    # --- state persistence ---
    def _read_state(self) -> dict[str, Any]:
        if not self._state_path.is_file():
            return {}
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_state(self, state: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._state_path.with_suffix(self._state_path.suffix + f".{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, self._state_path)

    # --- health ---
    def health(self) -> dict[str, Any]:
        conn = self._conn()
        try:
            stats = self._callback_store.callback_outbox_stats(conn)
            sideband_owner_freshness = (
                self._sideband_owner_freshness_status(conn)
                if self._transport == "sideband"
                else None
            )
        finally:
            conn.close()
        state = self._read_state()
        stats["bridge"] = "callback_bridge_v1"
        stats["startup_recovered_batch_count"] = self._startup_recovered_batch_count
        stats["executable"] = self._executable
        stats["last_delivery_error"] = state.get("last_error", "")
        stats["config"] = {
            "app_server_timeout_seconds": self._app_server_timeout,
            "lease_seconds": self._lease_seconds,
            "max_batch_members": self._max_batch_members,
            "transport": self._transport,
        }
        if sideband_owner_freshness is not None:
            stats["sideband_owner_freshness"] = sideband_owner_freshness
        return stats

    def status(self) -> dict[str, Any]:
        return self.health()

    def _sideband_owner_freshness_status(self, conn) -> dict[str, Any]:
        parked = self._sideband_parked_batch_rows(conn)
        return {
            "sideband_dir": str(self._sideband_dir),
            "parked_batch_count": len(parked),
            "parked_batches": parked,
        }

    def _sideband_parked_batch_rows(self, conn) -> list[dict[str, Any]]:
        try:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='callback_batches'"
            ).fetchone()
        except Exception:
            return []
        if table is None:
            return []
        columns = self._table_columns(conn, "callback_batches")
        required = {"batch_id", "origin_thread_id"}
        if not required.issubset(columns):
            return []
        select_cols = [
            "batch_id",
            "origin_thread_id",
            "state" if "state" in columns else "'' AS state",
            "last_failure_kind" if "last_failure_kind" in columns else "'' AS last_failure_kind",
            "updated_at" if "updated_at" in columns else "'' AS updated_at",
            "not_before_at" if "not_before_at" in columns else "'' AS not_before_at",
            "attempts" if "attempts" in columns else "0 AS attempts",
        ]
        where = "state='pending' AND last_failure_kind='busy'" if {"state", "last_failure_kind"}.issubset(columns) else "1=1"
        try:
            rows = conn.execute(
                f"SELECT {', '.join(select_cols)} FROM callback_batches WHERE {where} ORDER BY updated_at LIMIT 25"
            ).fetchall()
        except Exception:
            return []
        parked: list[dict[str, Any]] = []
        for row in rows:
            thread_id = str(row["origin_thread_id"])
            updated_at = str(row["updated_at"] or "")
            parked.append({
                "batch_id": str(row["batch_id"]),
                "origin_thread_id": redacted_thread_suffix(thread_id),
                "parked_reason": str(row["last_failure_kind"] or "waiting_for_thread_idle"),
                "parked_age_seconds": _age_seconds(updated_at),
                "not_before_at": str(row["not_before_at"] or ""),
                "attempts": int(row["attempts"] or 0),
                "owner_freshness": (
                    describe_sideband_owner_freshness(self._sideband_dir, thread_id, self._sideband_repo_id)
                    if self._sideband_repo_id
                    else {"owner_count": 0, "fresh_owner_count": 0, "ambiguous": False, "owners": []}
                ),
            })
        return parked

    def _table_columns(self, conn, table_name: str) -> set[str]:
        try:
            rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        except Exception:
            return set()
        return {str(row["name"]) for row in rows}

    # --- delivery ---
    def _process_batch(self, batch: CallbackBatch) -> bool:
        """Deliver one whole batch as a single turn. On success every
        member is marked delivered together.

        A busy/active-thread deferral (``BusyThreadError`` and its
        subclasses -- ``ActiveThreadSteerDeferralError`` and
        ``SidebandThreadBusyError``) reschedules the WHOLE batch
        non-blockingly via ``callback_batches.not_before_at`` (capped
        exponential backoff, computed here, applied in the DB -- this
        method never sleeps) and parks it forever: it can never be
        dead-lettered and never consumes the bounded dead-letter failure
        budget (B416 -- a temporary active/busy Codex thread must not be
        incorrectly dead-lettered after a handful of retries). A genuine
        transport/protocol failure (any other ``AppServerError``, e.g.
        ``SidebandUnavailableError``, ``SidebandRejectedError``, a crashed/
        unreachable App Server, or a malformed response) retains the
        existing bounded retry/dead-letter
        route on its own independent failure counter.
        """
        client_user_message_id = deterministic_batch_client_user_message_id(batch.batch_id)
        backoff_delay = min(
            DEFAULT_RETRY_BACKOFF_BASE ** max(0, batch.attempts - 1),
            DEFAULT_RETRY_BACKOFF_MAX,
        )
        conn = self._conn()
        try:
            try:
                self._deliver_batch_via_transport(batch, client_user_message_id)
                self._callback_store.mark_batch_delivered(conn, batch.batch_id)
                state = self._read_state()
                state["last_delivery_at"] = _utcnow()
                state["last_delivery_task_id"] = batch.members[0].task_id
                state["last_delivery_batch_size"] = batch.size
                state["last_error"] = ""
                self._write_state(state)
                return True
            except BusyThreadError as exc:
                reason = str(exc)[:500] or "thread_busy"
                self._callback_store.defer_batch_busy(conn, batch.batch_id, reason, delay_seconds=backoff_delay)
                return False
            except AppServerError as exc:
                error_msg = str(exc)[:500]
                if self._transport == "subprocess" and self._client is not None:
                    self._client.stop()
                    self._client = None
                self._callback_store.fail_batch_transient(
                    conn, batch.batch_id, error_msg,
                    max_retries=DEFAULT_MAX_RETRIES, delay_seconds=backoff_delay,
                )
                state = self._read_state()
                state["last_error"] = error_msg
                self._write_state(state)
                return False
        finally:
            conn.close()

    def _deliver_batch_via_transport(self, batch: CallbackBatch, client_user_message_id: str) -> None:
        if self._transport == "sideband":
            if self._sideband_client is None:
                self._sideband_client = SidebandCallbackClient(
                    repo_id=self._sideband_repo_id,
                    sideband_dir=self._sideband_dir, timeout=self._app_server_timeout,
                )
            self._sideband_client.deliver_callback_batch(
                batch.origin_thread_id,
                batch.as_prompt_members(),
                client_user_message_id=client_user_message_id,
                cwd=self._callback_cwd,
            )
            return
        if self._transport == "claude_cli":
            self._deliver_batch_via_claude_cli(batch, client_user_message_id)
            return
        if self._client is None or not self._client.alive:
            self._client = AppServerClient(
                self._executable, repo=self._repo, timeout=self._app_server_timeout,
            )
            self._client.start()
        self._client.deliver_callback_batch(
            batch.origin_thread_id,
            batch.as_prompt_members(),
            client_user_message_id=client_user_message_id,
            cwd=self._callback_cwd,
        )

    def _deliver_batch_via_claude_cli(self, batch: CallbackBatch, client_user_message_id: str) -> None:
        """Deliver via ``ClaudeCallbackAdapter`` in ``cli_resume`` mode.

        ``batch.origin_thread_id`` holds the persistent Claude ``session_id``
        (the existing origin_thread_id-keyed outbox row is reused as-is,
        per the B856 design note above -- no schema change). Resolves the
        exact repository/window/session identity through
        ``CoordinatorRouteKey`` (fail-closed, no cross-repository fallback)
        before ever invoking the CLI transport. ``raise_on_defer=True`` so
        a busy/unavailable outcome raises the matching ``BusyThreadError``
        subclass, letting the existing, unmodified ``_process_batch``
        busy/failure handling park or bounded-retry it exactly like Codex.
        """
        if self._claude_adapter is None:
            cli_client = ClaudeCliResumeClient(
                self._claude_executable,
                timeout=self._app_server_timeout,
                run_fn=self._claude_cli_run_fn,
            )
            self._claude_adapter = ClaudeCallbackAdapter(cli_client=cli_client)
        route = fail_on_incomplete_coordinator_route(
            repo_id=self._claude_repo_id,
            provider="claude",
            window_id=self._claude_window_id,
            task_id=batch.members[0].task_id,
            session_id=batch.origin_thread_id,
        )
        payload = {
            "members": batch.as_prompt_members(),
            "client_user_message_id": client_user_message_id,
        }
        state = self._read_state()
        state["last_claude_session_suffix"] = redacted_thread_suffix(batch.origin_thread_id)
        state["last_claude_attempt_at"] = _utcnow()
        try:
            self._claude_adapter.deliver_or_defer(route, payload=payload, raise_on_defer=True)
        except BusyThreadError as exc:
            state["last_claude_reason"] = str(exc)[:500]
            self._write_state(state)
            raise
        state["last_claude_reason"] = ""
        state["last_claude_delivery_at"] = state["last_claude_attempt_at"]
        self._write_state(state)

    def claude_status(self) -> dict[str, Any]:
        """Redacted-safe Claude callback status/health contract (B856):

        mode (panel/cli_resume/unavailable), a REDACTED bound session
        suffix (never the full session id), pending outbox age, the last
        delivery/deferral reason, and an overall health label. Shares the
        same durable outbox stats as ``health()`` -- never a second
        callback database.
        """
        conn = self._conn()
        try:
            stats = self._callback_store.callback_outbox_stats(conn)
        finally:
            conn.close()
        state = self._read_state()
        if self._transport == "claude_cli":
            mode = (self._claude_adapter.mode if self._claude_adapter is not None else "cli_resume")
        else:
            mode = "unavailable"
        pending = int((stats.get("by_state") or {}).get("pending", 0))
        return {
            "bridge": "callback_bridge_v1",
            "provider": "claude",
            "mode": mode,
            "transport": self._transport,
            "session_suffix": state.get("last_claude_session_suffix", "***"),
            "pending_count": pending,
            "last_delivery_at": state.get("last_claude_delivery_at", ""),
            "last_deferral_reason": state.get("last_claude_reason", ""),
            "health": "ok" if mode == "cli_resume" and pending == 0 else (
                "degraded" if mode == "cli_resume" else "unavailable"
            ),
        }

    def _claim_batch(self, conn) -> dict[str, Any] | None:
        # Repair any review transition written by an alternate lifecycle path
        # before claiming. This runs on every dispatcher iteration, so a new
        # task category can never silently enter Review without an outbox wake.
        if self._provider:
            self._callback_store.seed_missing_review_callbacks(
                conn, provider=self._provider
            )
        if self._provider:
            return self._callback_store.claim_pending_callback_batch(
                conn, self._lease_seconds, self._max_batch_members, provider=self._provider,
            )
        return self._callback_store.claim_pending_callback_batch(
            conn, self._lease_seconds, self._max_batch_members,
        )

    def run_once(self) -> dict[str, Any]:
        """Claim and deliver at most one BATCH (all currently-eligible
        pending rows for one origin_thread_id), never one turn per task."""
        conn = self._conn()
        try:
            claimed = self._claim_batch(conn)
        finally:
            conn.close()
        if claimed is None:
            return {"ok": True, "action": "no_pending"}
        batch = _batch_from_claim_result(claimed)
        delivered = self._process_batch(batch)
        if self._client is not None:
            self._client.stop()
            self._client = None
        return {
            "ok": delivered,
            "action": "delivered" if delivered else "deferred_or_failed",
            "batch_id": batch.batch_id,
            "batch_size": batch.size,
            "task_ids": [m.task_id for m in batch.members],
            "origin_thread_id": redacted_thread_suffix(batch.origin_thread_id),
        }

    def daemon(self, *, max_iterations: int | None = None) -> None:
        """Poll continuously. Idle polling starts zero Codex/model
        processes. Each iteration delivers at most one whole batch, so a
        thread with a large simultaneous backlog gets exactly one turn for
        it, and the next iteration immediately picks up any other thread's
        (or that thread's own next) backlog without an idle sleep."""
        self._stop_event.clear()
        consecutive_empty = 0
        iterations = 0
        while not self._stop_event.is_set():
            conn = self._conn()
            try:
                claimed = self._claim_batch(conn)
            finally:
                conn.close()
            if claimed is None:
                consecutive_empty += 1
                if consecutive_empty > 10 and self._client is not None:
                    self._client.stop()
                    self._client = None
                # Repo-local SQLite polling is cheap. A 30-second idle
                # backoff made healthy terminal callbacks look lost; bound
                # wake latency to two seconds.
                sleep_time = 1.0 if consecutive_empty <= 10 else 2.0
                self._stop_event.wait(sleep_time)
            else:
                consecutive_empty = 0
                self._process_batch(_batch_from_claim_result(claimed))
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
        if self._client is not None:
            self._client.stop()
            self._client = None

    def stop_daemon(self) -> None:
        self._stop_event.set()
        if self._sideband_client is not None:
            self._sideband_client.stop()

    def dry_run(self, task_id: str, state: str, *, thread_id: str = "") -> dict[str, Any]:
        """Validate the callback pipeline without sending a real turn.

        If ``thread_id`` is provided, validates the protocol up to (but not
        including) ``turn/start`` -- a safe canary that never sends a turn.
        """
        prompt = build_callback_prompt(task_id, state)
        cmd = build_app_server_command(self._executable)
        result: dict[str, Any] = {
            "ok": True,
            "command": cmd,
            "prompt": prompt,
            "prompt_length": len(prompt),
            "task_id": task_id,
            "state": state,
            "rejected_flags_absent": all(flag not in cmd for flag in _REJECTED_FLAGS),
        }
        if thread_id:
            client = AppServerClient(self._executable, repo=self._repo)
            try:
                client.start()
                client.initialize()
                client.send_initialized()
                client.thread_resume(thread_id)
                result["thread_resume_ok"] = True
                result["thread_id_suffix"] = redacted_thread_suffix(thread_id)
            except AppServerError as exc:
                result["ok"] = False
                result["error"] = str(exc)[:500]
            finally:
                client.stop()
        return result


# ---------------------------------------------------------------------------
# B857: repository-bound dispatcher lifecycle.
#
# The bridge/outbox/transport machinery above already existed (B343/B384/
# B402/B407/B409/B416/B856); it only ever ran through manually-invoked CLI
# commands (run-once/daemon/status/dry-run). This section is the missing
# piece: a single, idempotent, repo-bound owner of exactly one background
# ``CallbackBridge.daemon()`` loop, started/stopped by the VS Code
# extension's process lifecycle (one ``python -m aiworkhub.server`` process
# per active repository -- see ``vscode-extension/extension.js::
# McpStdioClient``) rather than a systemd unit, a manual daemon command, or
# an HTTP dashboard server. Never adds a new durable store: routing still
# goes through the existing AppServerClient/SidebandCallbackClient (Codex)
# or ClaudeCliResumeClient/ClaudeCallbackAdapter (Claude) transports on the
# existing package-local ``callback_store.py`` outbox.
# ---------------------------------------------------------------------------

# Providers this dispatcher will actually spin a delivery thread for.
# "copilot" is a recognized route-identity provider (route_identity.py) but
# has no wired transport yet -- selecting it must fail closed (no thread,
# visible error) rather than silently doing nothing or fabricating delivery.
SUPPORTED_DISPATCH_PROVIDERS: frozenset[str] = frozenset({"codex", "claude"})

# B875: anchored on ``sys`` (a module the interpreter itself never reloads
# or evicts from ``sys.modules``) rather than on this module's own globals.
# Some test fixtures (e.g. the B844 bundled-runtime fallback harness) purge
# every ``aiworkhub.*`` entry from ``sys.modules`` around an in-process
# re-import to exercise a clean-import code path; a plain module-global dict
# would then silently fork into two disconnected registries -- the stale
# one already bound by any test module that imported ``callback_bridge``
# before the purge, and a fresh empty one picked up by every subsequent
# (re-)import, including lazy ones like ``core._callback_bridge_module()``.
# Anchoring the (lock, dict) pair on ``sys`` under a private attribute name
# guarantees every generation of this module -- old or freshly re-imported
# -- resolves to the exact same lock and the exact same registry for the
# life of the process, so "exactly one dispatcher per repository" holds
# regardless of how many times ``aiworkhub.callback_bridge`` gets reloaded.
_DISPATCHER_REGISTRY_ANCHOR_ATTR = "_aiworkhub_callback_dispatcher_registry_v1"


def _dispatcher_registry_state() -> tuple[threading.Lock, dict[str, "CallbackDispatcher"]]:
    anchor = getattr(sys, _DISPATCHER_REGISTRY_ANCHOR_ATTR, None)
    if anchor is None:
        anchor = (threading.Lock(), {})
        setattr(sys, _DISPATCHER_REGISTRY_ANCHOR_ATTR, anchor)
    return anchor


_DISPATCHER_REGISTRY_LOCK, _DISPATCHER_REGISTRY = _dispatcher_registry_state()


def _dispatcher_registry_key(repo_root: Path | str) -> str:
    return str(Path(repo_root).resolve())


class CallbackDispatcher:
    """Owns exactly one background ``CallbackBridge.daemon()`` thread for
    one repository, bound to one explicitly selected coordinator provider.

    Never constructed directly by callers outside this module's registry
    functions (``ensure_dispatcher`` / ``stop_dispatcher``) -- that registry
    is what makes "exactly one dispatcher per repository" hold across
    activation, tab-deserialization, and reload, since every one of those
    events resolves to the same repo-root key.
    """

    def __init__(
        self,
        repo_root: Path | str,
        provider: str,
        *,
        repo_id: str = "",
        window_id: str = "",
        bridge_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.provider = str(provider or "").strip().lower()
        self.repo_id = repo_id
        self.window_id = window_id
        self._bridge_kwargs = dict(bridge_kwargs or {})
        self._bridge: CallbackBridge | None = None
        self._thread: threading.Thread | None = None
        self._start_error: str = ""
        self._started_at: str = ""
        self._lock = threading.Lock()

    @property
    def supported(self) -> bool:
        return self.provider in SUPPORTED_DISPATCH_PROVIDERS

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def start(self) -> None:
        """Idempotent: a second call while already running is a no-op."""
        with self._lock:
            if self.is_running():
                return
            if not self.supported:
                # Fail closed, visibly -- no thread, no fabricated delivery.
                self._start_error = f"unsupported_provider:{self.provider or 'unset'}"
                self._bridge = None
                self._thread = None
                return
            kwargs: dict[str, Any] = dict(self._bridge_kwargs)
            kwargs["repo"] = self.repo_root
            kwargs["provider"] = self.provider
            # B925: harmless when the resolved transport isn't "sideband"
            # (CallbackBridge only enforces/uses it in that case) but
            # always available so a future switch to the sideband
            # transport for this dispatcher never silently starts unbound.
            kwargs.setdefault("sideband_repo_id", self.repo_id)
            if self.provider == "codex":
                # Existing canonical Codex transport (AppServerClient by
                # default; "sideband" may be supplied explicitly through
                # bridge_kwargs once the extension-owned App Server socket
                # is wired as the live default -- unchanged B409 contract).
                kwargs.setdefault("transport", "subprocess")
                if kwargs["transport"] == "sideband":
                    # Sideband is a bounded local socket round-trip to the
                    # extension-owned mux, not a long-running model turn.
                    # Reusing the subprocess transport's 35-minute lease made
                    # abrupt reloads strand every later callback behind one
                    # orphaned inflight batch.
                    kwargs.setdefault("app_server_timeout", SIDEBAND_APP_SERVER_TIMEOUT_SECONDS)
                    kwargs.setdefault("lease_seconds", SIDEBAND_LEASE_SECONDS)
                    kwargs.setdefault("lease_margin_seconds", SIDEBAND_LEASE_MARGIN_SECONDS)
            elif self.provider == "claude":
                # Existing canonical Claude transport (B856): claude_cli,
                # which requires a bound repo_id/window_id at construction
                # (fails closed otherwise -- see CallbackBridge.__init__).
                kwargs["transport"] = "claude_cli"
                kwargs.setdefault("claude_repo_id", self.repo_id)
                kwargs.setdefault("claude_window_id", self.window_id)
            try:
                bridge = CallbackBridge(**kwargs)
                bridge.recover_incompatible_transport_leases()
            except Exception as exc:  # noqa: BLE001 -- surfaced via health(), never raised to caller
                self._start_error = f"construction_failed:{type(exc).__name__}:{exc}"[:500]
                self._bridge = None
                self._thread = None
                return
            self._bridge = bridge
            self._start_error = ""
            self._started_at = _utcnow()
            thread = threading.Thread(
                target=bridge.daemon,
                name=f"aiworkhub-callback-dispatcher:{self.provider}",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        with self._lock:
            bridge = self._bridge
            thread = self._thread
        if bridge is not None:
            bridge.stop_daemon()
        if thread is not None:
            thread.join(timeout=timeout)
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def health(self) -> dict[str, Any]:
        running = self.is_running()
        out: dict[str, Any] = {
            "ok": self.supported,
            "dispatcher_running": running,
            "provider": self.provider,
            "provider_supported": self.supported,
            "repo_root": str(self.repo_root),
            "repo_id": self.repo_id,
            "started_at": self._started_at,
            "last_start_error": self._start_error,
        }
        bridge = self._bridge
        if bridge is not None:
            try:
                bridge_health = bridge.health()
            except Exception as exc:  # noqa: BLE001 -- health must never raise
                bridge_health = {"ok": False, "error": f"health_failed:{type(exc).__name__}"}
            out["bridge"] = bridge_health
            by_state = bridge_health.get("by_state")
            out["pending_count"] = int(by_state.get("pending", 0)) if isinstance(by_state, dict) else 0
            out["last_delivery_error"] = bridge_health.get("last_delivery_error", "")
        else:
            out["bridge"] = None
            out["pending_count"] = 0
            out["last_delivery_error"] = self._start_error
        return out


def ensure_dispatcher(
    repo_root: Path | str,
    provider: str,
    *,
    repo_id: str = "",
    window_id: str = "",
    bridge_kwargs: Mapping[str, Any] | None = None,
) -> CallbackDispatcher:
    """Idempotently start (or return) the ONE dispatcher for ``repo_root``.

    Safe to call repeatedly from activation, tab-deserialization, and
    reload: a call that matches the currently-registered provider is a
    no-op; a call with a DIFFERENT provider (explicit coordinator-target
    switch) stops the old dispatcher first so a stale thread never keeps
    routing to the previous transport.
    """
    key = _dispatcher_registry_key(repo_root)
    provider = str(provider or "").strip().lower()
    with _DISPATCHER_REGISTRY_LOCK:
        existing = _DISPATCHER_REGISTRY.get(key)
        if existing is not None and existing.provider == provider:
            existing.start()  # no-op if already running
            return existing
        if existing is not None:
            existing.stop()
        dispatcher = CallbackDispatcher(
            repo_root, provider, repo_id=repo_id, window_id=window_id, bridge_kwargs=bridge_kwargs
        )
        _DISPATCHER_REGISTRY[key] = dispatcher
    dispatcher.start()
    return dispatcher


def get_dispatcher(repo_root: Path | str) -> CallbackDispatcher | None:
    with _DISPATCHER_REGISTRY_LOCK:
        return _DISPATCHER_REGISTRY.get(_dispatcher_registry_key(repo_root))


def stop_dispatcher(repo_root: Path | str) -> bool:
    """Stop and unregister the dispatcher for ``repo_root``, if any.

    Returns ``True`` iff a dispatcher was actually registered (and is now
    stopped/unregistered). Called on workspace/repository switch and on
    extension deactivation so no cross-repository read or delivery can
    happen after the switch.
    """
    key = _dispatcher_registry_key(repo_root)
    with _DISPATCHER_REGISTRY_LOCK:
        dispatcher = _DISPATCHER_REGISTRY.pop(key, None)
    if dispatcher is None:
        return False
    dispatcher.stop()
    return True


def stop_all_dispatchers() -> int:
    """Stop every registered dispatcher. Used by extension deactivate()."""
    with _DISPATCHER_REGISTRY_LOCK:
        dispatchers = list(_DISPATCHER_REGISTRY.values())
        _DISPATCHER_REGISTRY.clear()
    for dispatcher in dispatchers:
        dispatcher.stop()
    return len(dispatchers)


def dispatcher_health(repo_root: Path | str) -> dict[str, Any]:
    """Read-only health for the repo's dispatcher, or a not-running shape
    if none is registered yet (never an error -- an unregistered dispatcher
    on an otherwise-healthy repository is a normal, not-degraded state)."""
    dispatcher = get_dispatcher(repo_root)
    if dispatcher is None:
        return {
            "ok": True,
            "dispatcher_running": False,
            "provider": "",
            "provider_supported": False,
            "repo_root": str(Path(repo_root)),
            "repo_id": "",
            "started_at": "",
            "last_start_error": "",
            "bridge": None,
            "pending_count": 0,
            "last_delivery_error": "",
            "registered": False,
        }
    out = dispatcher.health()
    out["registered"] = True
    return out


def main(argv: list[str] | None = None) -> int:
    """``aiworkhub-callback-bridge run-once|daemon|status|dry-run`` CLI.

    ``--app-server-timeout-seconds``/``--lease-seconds``/
    ``--max-batch-members`` (or the ``AIWORKHUB_CALLBACK_APP_SERVER_TIMEOUT_SECONDS``/
    ``AIWORKHUB_CALLBACK_LEASE_SECONDS``/``AIWORKHUB_CALLBACK_MAX_BATCH_MEMBERS`` env
    vars) override the safe defaults; an invalid lease/timeout combination
    (lease must be >= timeout + margin) is rejected before anything starts.
    """
    argv = list(argv if argv is not None else sys.argv[1:])
    executable = "codex"
    if "--executable" in argv:
        idx = argv.index("--executable")
        executable = argv[idx + 1]
        del argv[idx:idx + 2]

    transport = os.environ.get(ENV_TRANSPORT, "subprocess")
    if "--transport" in argv:
        idx = argv.index("--transport")
        if idx + 1 >= len(argv):
            print("invalid callback bridge configuration: --transport requires a value", file=sys.stderr)
            return 2
        transport = argv[idx + 1]
        del argv[idx:idx + 2]
    if transport not in ("subprocess", "sideband"):
        print(f"invalid callback bridge transport: {transport}", file=sys.stderr)
        return 2

    try:
        argv, app_server_timeout, lease_seconds, max_batch_members = resolve_bridge_settings(argv)
    except ValueError as exc:
        print(f"invalid callback bridge configuration: {exc}", file=sys.stderr)
        return 2

    if not argv:
        print(
            "usage: aiworkhub-callback-bridge {run-once|daemon|status|dry-run} "
            "[--transport subprocess|sideband] [--executable BIN] "
            "[--app-server-timeout-seconds N] [--lease-seconds N] "
            "[--max-batch-members N]",
            file=sys.stderr,
        )
        return 2

    bridge = CallbackBridge(
        executable=executable,
        app_server_timeout=app_server_timeout,
        lease_seconds=lease_seconds,
        max_batch_members=max_batch_members,
        transport=transport,
    )
    command = argv[0]
    if command == "run-once":
        print(json.dumps(bridge.run_once(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if command == "daemon":
        bridge.daemon()
        return 0
    if command == "status":
        print(json.dumps(bridge.status(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if command == "dry-run":
        if len(argv) < 3:
            print("usage: aiworkhub-callback-bridge dry-run TASK_ID STATE", file=sys.stderr)
            return 2
        result = bridge.dry_run(argv[1], argv[2])
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 1
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


__all__ = [
    "CALLBACK_ELIGIBLE_STATES",
    "CALLBACK_CWD",
    "DEFAULT_LEASE_MARGIN_SECONDS",
    "ActiveThreadSteerDeferralError",
    "AppServerClient",
    "AppServerError",
    "BusyThreadError",
    "CallbackBatch",
    "CallbackBridge",
    "CallbackDeliveryResult",
    "CallbackDispatcher",
    "CallbackEntry",
    "ClaudeCallbackAdapter",
    "ClaudeCliBusyError",
    "ClaudeCliError",
    "ClaudeCliProtocolError",
    "ClaudeCliResumeClient",
    "ClaudeCliUnavailableError",
    "ClaudePanelDeferralError",
    "build_app_server_command",
    "build_claude_resume_command",
    "build_batch_callback_prompt",
    "build_callback_prompt",
    "deterministic_batch_client_user_message_id",
    "deterministic_client_user_message_id",
    "main",
    "redacted_thread_suffix",
    "resolve_bridge_settings",
    "select_steer_target",
    "validate_lease_and_timeout",
    "SidebandCallbackClient",
    "_is_sideband_turn_start_busy_rejection",
    "SidebandNotReadyError",
    "SidebandOwnerAmbiguousError",
    "SidebandOwnerNotFoundError",
    "SidebandOwnerResolutionError",
    "SidebandRejectedError",
    "SidebandThreadBusyError",
    "SidebandUnavailableError",
    "SUPPORTED_DISPATCH_PROVIDERS",
    "dispatcher_health",
    "ensure_dispatcher",
    "get_dispatcher",
    "stop_all_dispatchers",
    "stop_dispatcher",
]


if __name__ == "__main__":
    raise SystemExit(main())
