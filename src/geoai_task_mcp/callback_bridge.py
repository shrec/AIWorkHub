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
  transport/protocol failures (see ``AITools/taskdb.py::defer_batch_busy``
  vs ``fail_batch_transient`` and ``callback_batches.not_before_at``).
- Run-once, daemon, status, and dry-run commands.
"""
from __future__ import annotations

import contextlib
import json
import os
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
from typing import Any

from .app_server_mux import (
    SIDEBAND_REQUEST_DEADLINE_SECONDS as DEFAULT_SIDEBAND_TIMEOUT,
    SIDEBAND_RESPONSE_MAX_BYTES,
    default_sideband_dir,
    describe_sideband_owner_freshness,
    find_owning_sideband_instances,
)

CALLBACK_ELIGIBLE_STATES: frozenset[str] = frozenset({
    "review_ready",
    "blocked",
    "launch_failed",
    "validation_failed",
    "scope_rejected",
    "timed_out",
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
REDACTED_SUFFIX_LENGTH = 4
CALLBACK_CWD = "/home/shrek/GeoAI"

# Env var overrides for CLI-configurable timeout/lease (see main()/
# resolve_bridge_settings()). CLI flags take precedence over these.
ENV_APP_SERVER_TIMEOUT_SECONDS = "GEOAI_CALLBACK_APP_SERVER_TIMEOUT_SECONDS"
ENV_LEASE_SECONDS = "GEOAI_CALLBACK_LEASE_SECONDS"
ENV_MAX_BATCH_MEMBERS = "GEOAI_CALLBACK_MAX_BATCH_MEMBERS"
ENV_TRANSPORT = "GEOAI_CALLBACK_TRANSPORT"

# Rejected transport surface: never invoked, never emulated.
_REJECTED_FLAGS = ("--thread-id", "--client-id", "--no-remote")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{0,128}$")


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
    ``AITools/taskdb.py::claim_pending_callback_batch``), so hashing it
    alone is sufficient: a bridge restart mid-delivery reclaims the SAME
    batch_id (never re-forms a new one for an already-leased batch) and
    therefore always reissues the identical clientUserMessageId.
    """
    import hashlib

    return "cbbatch_" + hashlib.sha256(batch_id.encode("utf-8")).hexdigest()[:32]


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
        sideband_dir: Path | str | None = None,
        timeout: float = DEFAULT_SIDEBAND_TIMEOUT,
    ) -> None:
        self._sideband_dir = Path(sideband_dir) if sideband_dir else default_sideband_dir()
        self._timeout = timeout

    def _resolve_owner(self, thread_id: str):
        instances = find_owning_sideband_instances(self._sideband_dir, thread_id)
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
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        try:
            try:
                sock.connect(str(owner.socket_path))
            except OSError as exc:
                raise SidebandUnavailableError(f"sideband socket unreachable: {exc}") from exc
            payload = json.dumps({"cap": token, "method": method, "params": params}, ensure_ascii=False)
            try:
                sock.sendall((payload + "\n").encode("utf-8"))
                sock.shutdown(_socket.SHUT_WR)
            except OSError as exc:
                raise SidebandUnavailableError(f"sideband write failed: {exc}") from exc
            raw = self._recv_line(sock)
        finally:
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
            if self._process.poll() is not None:
                raise AppServerError(
                    f"app server exited with code {self._process.returncode}"
                )
            stdout = self._process.stdout
            if stdout is None:
                raise AppServerError("app server stdout pipe is unavailable")
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
                    "name": "geoai-task-mcp-callback-bridge",
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
        """Start a turn and wait for the matching ``turn/completed`` notification.

        ``TurnStartParams`` requires ``threadId`` and ``input`` (an array of
        ``UserInput``); ``cwd``/``clientUserMessageId`` are optional. The
        synchronous response contains ``result.turn.id``. Completion is
        signalled later by an async ``turn/completed`` notification and is
        accepted only when both ``threadId`` and ``turn.id`` match the
        acknowledged turn.
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
        acknowledged_turn_id: str | None = None
        completed: dict[str, Any] | None = None
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
                if completed is not None:
                    completed_turn = (completed.get("params") or {}).get("turn") or {}
                    if completed_turn.get("id") != acknowledged_turn_id:
                        raise AppServerError("turn/completed turn identity mismatch")
                    if completed_turn.get("status") != "completed":
                        raise AppServerError("turn/completed reported non-completed status")
                    return completed
                continue
            if msg.get("method") == "turn/completed":
                params_out = msg.get("params") or {}
                if params_out.get("threadId") == thread_id:
                    completed = msg
                    completed_turn = params_out.get("turn") or {}
                    if acknowledged_turn_id is not None:
                        if completed_turn.get("id") != acknowledged_turn_id:
                            raise AppServerError("turn/completed turn identity mismatch")
                        if completed_turn.get("status") != "completed":
                            raise AppServerError("turn/completed reported non-completed status")
                        return completed
                continue  # a stray notification for a different thread
            # Ignore other interleaved notifications (turn/started,
            # turn/diff/updated, turn/plan/updated, etc.) while waiting.
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


# --- Outbox entry (mirrors AITools/taskdb.py::callback_outbox row shape) ---

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


def _taskdb_module():
    """Import ``AITools/taskdb.py`` regardless of caller's sys.path state."""
    import importlib

    try:
        return importlib.import_module("taskdb")
    except ImportError:
        pass
    aitools_dir = Path(__file__).resolve().parents[4] / "AITools"
    if str(aitools_dir) not in sys.path:
        sys.path.insert(0, str(aitools_dir))
    return importlib.import_module("taskdb")


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

    The durable outbox itself lives in ``AITools/taskdb.py`` (SQLite,
    shared with taskctl) -- this class only claims/delivers/finalizes
    entries through that module's primitives, so the bridge and taskctl
    never disagree about outbox state.
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
    ):
        # lease_margin_seconds is a deliberate, visible constructor override
        # for tests/dev exercising short lease-expiry scenarios; the CLI
        # (main()/resolve_bridge_settings()) never exposes it, so the
        # production safety margin is never operator-configurable away.
        validate_lease_and_timeout(lease_seconds, app_server_timeout, margin_seconds=lease_margin_seconds)
        if transport not in ("subprocess", "sideband"):
            raise ValueError(f"unknown callback transport: {transport}")
        self._taskdb = _taskdb_module()
        self._repo = Path(repo) if repo else Path(getattr(self._taskdb, "REPO", Path.cwd()))
        self._db_path = Path(db_path) if db_path else self._taskdb.DEFAULT_DB
        self._state_path = Path(state_path) if state_path else (
            self._repo / "tools" / "geoai-task-mcp" / "logs" / "callback_bridge_state.json"
        )
        self._executable = executable
        self._lease_seconds = lease_seconds
        self._app_server_timeout = app_server_timeout
        self._callback_cwd = callback_cwd
        self._max_batch_members = int(max_batch_members) if max_batch_members else int(
            getattr(self._taskdb, "DEFAULT_CALLBACK_BATCH_MAX_MEMBERS", 25)
        )
        # "subprocess" (default, unchanged B384/B402/B407 behavior) spawns
        # its own AppServerClient over a fresh `codex app-server` child --
        # correct only while no extension owns the thread already.
        # "sideband" (B409) instead reaches the extension-owned App Server
        # through app_server_mux.py's local socket; Codex wires this in as
        # the live default only after the VS Code setting + canary land.
        self._transport = transport
        self._sideband_dir = Path(sideband_dir) if sideband_dir else default_sideband_dir()
        self._client: AppServerClient | None = None
        self._sideband_client: SidebandCallbackClient | None = None
        self._stop_event = threading.Event()

    def _conn(self):
        conn = self._taskdb.open_db(self._db_path)
        self._taskdb.init_db(conn)
        return conn

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
            stats = self._taskdb.callback_outbox_stats(conn)
            sideband_owner_freshness = (
                self._sideband_owner_freshness_status(conn)
                if self._transport == "sideband"
                else None
            )
        finally:
            conn.close()
        state = self._read_state()
        stats["bridge"] = "callback_bridge_v1"
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
                "owner_freshness": describe_sideband_owner_freshness(self._sideband_dir, thread_id),
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
                self._taskdb.mark_batch_delivered(conn, batch.batch_id)
                state = self._read_state()
                state["last_delivery_at"] = _utcnow()
                state["last_delivery_task_id"] = batch.members[0].task_id
                state["last_delivery_batch_size"] = batch.size
                state["last_error"] = ""
                self._write_state(state)
                return True
            except BusyThreadError as exc:
                reason = str(exc)[:500] or "thread_busy"
                self._taskdb.defer_batch_busy(conn, batch.batch_id, reason, delay_seconds=backoff_delay)
                return False
            except AppServerError as exc:
                error_msg = str(exc)[:500]
                if self._transport != "sideband" and self._client is not None:
                    self._client.stop()
                    self._client = None
                self._taskdb.fail_batch_transient(
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
                    sideband_dir=self._sideband_dir, timeout=self._app_server_timeout,
                )
            self._sideband_client.deliver_callback_batch(
                batch.origin_thread_id,
                batch.as_prompt_members(),
                client_user_message_id=client_user_message_id,
                cwd=self._callback_cwd,
            )
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

    def _claim_batch(self, conn) -> dict[str, Any] | None:
        return self._taskdb.claim_pending_callback_batch(
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
                sleep_time = 1.0 if consecutive_empty <= 10 else min(30.0, consecutive_empty * 2.0)
                time.sleep(sleep_time)
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


def main(argv: list[str] | None = None) -> int:
    """``geoai-task-callback-bridge run-once|daemon|status|dry-run`` CLI.

    ``--app-server-timeout-seconds``/``--lease-seconds``/
    ``--max-batch-members`` (or the ``GEOAI_CALLBACK_APP_SERVER_TIMEOUT_SECONDS``/
    ``GEOAI_CALLBACK_LEASE_SECONDS``/``GEOAI_CALLBACK_MAX_BATCH_MEMBERS`` env
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
            "usage: geoai-task-callback-bridge {run-once|daemon|status|dry-run} "
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
            print("usage: geoai-task-callback-bridge dry-run TASK_ID STATE", file=sys.stderr)
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
    "CallbackEntry",
    "build_app_server_command",
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
]


if __name__ == "__main__":
    raise SystemExit(main())
