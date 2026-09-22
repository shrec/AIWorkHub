"""The agentic-CLI backend behind the manager loop's ``ManagerBackend`` protocol.

RM-2026-00067 phase 1, card 2. :mod:`aiworkhub.manager_loop` owns the
provider-neutral half -- the session record, the rehydration brief, the
orchestrator -- and launches nothing. This module is the other half: one
manager conversation carried by one agentic CLI (``claude_cli``, ``codex_cli``
or ``opencode_cli``) on the HOST. There is no AppContainer, no Landlock jail
and no worker MCP runtime here; those belong to the worker lane, which claims a
card and confines it. The manager is the operator's own seat.

Reused, found through Source Graph rather than respelled:

* :func:`aiworkhub.runtime_adapters.build_runtime_command` -- the per-adapter
  argv/cwd plan the worker lane already builds: ``--output-format stream-json``
  for Claude, ``exec --json`` for Codex, ``run --format json`` for OpenCode,
  non-interactive, with model selection and executable resolution.
* :func:`aiworkhub.runtime_adapters.classify_provider_outcome` -- the named
  verdict for a non-zero provider exit, carried into the ``error`` event.
* :func:`aiworkhub.context_capture._message_text` -- the existing projection of
  a Claude message's completed text blocks into one string.
* :func:`aiworkhub.workforce_catalog.catalog_declares_route` -- the existing
  workforce registry, which decides whether ``(backend_id, model)`` is a route
  this repository actually declares.
* :func:`aiworkhub.platform_io.terminate_process_tree` and
  :func:`aiworkhub.platform_io.is_windows` -- the process kill facade and the
  one platform predicate. Nothing here calls ``os.kill``; nothing runs through
  a shell; every launch is an argv list.

Deliberately NOT reused, with the reason:

* :func:`aiworkhub.provider_usage.read_provider_usage` summarizes a *completed*
  stdout log by path. A turn must emit events while the process still runs, so
  the terminal event's own provider-reported ``usage`` mapping is carried
  through unchanged instead of being re-derived after exit.
* :func:`aiworkhub.manager_transcript_capture.extract_codex_completed_message`
  parses the Codex App Server mux JSON-RPC protocol (``item/completed``), not
  the ``codex exec --json`` stream this backend reads.
* ``process_launcher.ProcessManager`` is worker-only: it claims a card and
  provisions a sandbox and a worker MCP runtime around every launch. A manager
  turn is none of those, so this module spawns its own child directly.

A line the translator does not recognize is skipped, never fatal. Each turn is
planned by :func:`runtime_adapters.build_manager_command`, the host-side plan:
a worker is confined, the owner's manager seat is not.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Sequence

from . import context_capture, platform_io, runtime_adapters, workforce_catalog
from .manager_loop import ManagerLoopError

MANAGER_BACKEND_IDS: tuple[str, ...] = ("claude_cli", "codex_cli", "opencode_cli")
# The worker lane's own launch ceiling, reused as the manager turn default: a
# manager turn is one non-interactive CLI run of the same shape.
DEFAULT_TURN_TIMEOUT_SECONDS = 3600.0
MAX_ERROR_DETAIL_CHARS = 500
MAX_STDERR_CHARS = 2000
REAP_TIMEOUT_SECONDS = 5.0

# The conversation id spellings the three CLIs report; the first non-empty one
# seen in a turn is what the next turn resumes.
_CONVERSATION_KEYS = ("session_id", "sessionID", "thread_id", "threadId", "conversation_id")
# Claude's worker argv forbids session persistence, which is exactly what a
# multi-turn manager conversation needs, so this one token is dropped.
_CLAUDE_NO_PERSIST = "--no-session-persistence"
_DROPPED_TOKENS: Mapping[str, frozenset[str]] = MappingProxyType(
    {"claude_cli": frozenset({_CLAUDE_NO_PERSIST})}
)
# Where each CLI's grammar allows its resume tokens: after the executable for
# Claude, after the ``exec``/``run`` subcommand for Codex and OpenCode.
_RESUME_GRAMMAR: Mapping[str, tuple[int, str]] = MappingProxyType(
    {"claude_cli": (1, "--resume"), "codex_cli": (2, "resume"), "opencode_cli": (2, "--session")}
)
# Only Claude has a verified argv spelling for an MCP config in this repository;
# OpenCode takes its config through OPENCODE_CONFIG_CONTENT instead, so a config
# asked for on another backend is refused rather than silently dropped.
_MCP_CONFIG_FLAGS: Mapping[str, str] = MappingProxyType({"claude_cli": "--mcp-config"})
_CODEX_TOOL_ITEMS = frozenset({"command_execution", "mcp_tool_call", "file_change", "web_search"})


def _turn_error(source: str, detail: str) -> dict[str, Any]:
    """One loop ``error`` event, bounded so a hostile provider cannot flood the log."""
    payload = {"source": source, "error": str(detail)[:MAX_ERROR_DETAIL_CHARS]}
    return {"type": "error", "payload": payload}


def _turn_end(usage: Any) -> dict[str, Any]:
    """The turn's final event; ``usage`` appears only when the provider reported it."""
    reported = dict(usage) if isinstance(usage, Mapping) and usage else None
    return {"type": "turn_end", "payload": {"usage": reported} if reported else {}}


def _assistant_text(value: Any) -> list[dict[str, Any]]:
    text = str(value or "").strip()
    return [{"type": "assistant_text", "payload": {"text": text}}] if text else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _claude_events(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Claude ``stream-json``: assistant/user content blocks, then the result event."""
    if str(event.get("type") or "") == "result":
        return [_turn_end(event.get("usage"))]
    content = _mapping(event.get("message")).get("content")
    events = _assistant_text(context_capture._message_text(content))
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "tool_use":
            events.append({
                "type": "tool_call",
                "payload": {"name": str(block.get("name") or ""), "input": block.get("input")},
            })
        elif block.get("type") == "tool_result":
            events.append({
                "type": "tool_result",
                "payload": {
                    "name": str(block.get("tool_use_id") or ""),
                    "output": block.get("content"),
                },
            })
    return events


def _codex_events(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Codex ``exec --json``: item events, then the completed turn's usage."""
    kind = str(event.get("type") or "")
    if kind == "turn.completed":
        return [_turn_end(event.get("usage"))]
    item = _mapping(event.get("item"))
    item_type = str(item.get("type") or "")
    if not kind.startswith("item.") or not item_type:
        return []
    if item_type in ("agent_message", "agentMessage"):
        return _assistant_text(item.get("text"))
    if item_type not in _CODEX_TOOL_ITEMS:
        return []
    if kind == "item.completed":
        has_aggregate = "aggregated_output" in item
        output = item.get("aggregated_output") if has_aggregate else item.get("output")
        return [{"type": "tool_result", "payload": {"name": item_type, "output": output}}]
    request = item.get("command") if "command" in item else item.get("arguments")
    return [{"type": "tool_call", "payload": {"name": item_type, "input": request}}]


def _opencode_events(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """OpenCode ``run --format json``: text and tool parts, then ``step_finish``."""
    kind = str(event.get("type") or "")
    if kind == "step_finish":
        return [_turn_end(event.get("tokens"))]
    part = _mapping(event.get("part"))
    if kind == "text":
        return _assistant_text(part.get("text"))
    if kind != "tool":
        return []
    state = _mapping(part.get("state"))
    name = str(part.get("tool") or "")
    if str(state.get("status") or "") in ("completed", "error"):
        return [{"type": "tool_result", "payload": {"name": name, "output": state.get("output")}}]
    return [{"type": "tool_call", "payload": {"name": name, "input": state.get("input")}}]


_TRANSLATORS: Mapping[str, Callable[[Mapping[str, Any]], list[dict[str, Any]]]] = MappingProxyType(
    {
        "claude_cli": _claude_events,
        "codex_cli": _codex_events,
        "opencode_cli": _opencode_events,
    }
)


def _provider_error(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """The provider's own failure report, or ``None`` when the event is not one."""
    if str(event.get("type") or "") != "error" and not event.get("is_error"):
        return None
    reported = event.get("error")
    if isinstance(reported, Mapping):
        detail = reported.get("name") or reported.get("message") or "provider_error"
    else:
        detail = reported or event.get("subtype") or "provider_error"
    return _turn_error("provider", str(detail))


def translate(backend_id: str, event: Any) -> list[dict[str, Any]]:
    """One provider event as loop events; an unrecognized one yields nothing at all."""
    if not isinstance(event, Mapping):
        return []
    failure = _provider_error(event)
    if failure is not None:
        return [failure]
    translator = _TRANSLATORS.get(backend_id)
    return translator(event) if translator is not None else []


def conversation_id_of(event: Mapping[str, Any]) -> str:
    """The provider conversation id this event reports, or ``""`` when it reports none."""
    for key in _CONVERSATION_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def resume_argv(backend_id: str, argv: Sequence[str], conversation_id: str) -> list[str]:
    """Insert this CLI's resume tokens where its own grammar requires them."""
    tokens = list(argv)
    if not conversation_id or backend_id not in _RESUME_GRAMMAR:
        return tokens
    index, flag = _RESUME_GRAMMAR[backend_id]
    return tokens[:index] + [flag, conversation_id] + tokens[index:]


def _spawn_cli(argv: Sequence[str], cwd: str | None) -> Any:
    """Start one non-interactive CLI turn in its own process group; argv list, no shell."""
    grouping: dict[str, Any] = (
        {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        if platform_io.is_windows()
        else {"start_new_session": True}
    )
    # The argv list comes from build_runtime_command; shell=False is the default
    # and is never overridden, so no provider text is ever interpreted as a command.
    return subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **grouping,
    )


def _decode(raw: str) -> Mapping[str, Any] | None:
    """One stdout line as a JSON object; anything else is skipped, never fatal."""
    line = raw.strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _reap(process: Any) -> int:
    """Wait briefly for an exit code; an unreapable child reports -1, never hangs."""
    try:
        return int(process.wait(timeout=REAP_TIMEOUT_SECONDS))
    except (subprocess.TimeoutExpired, OSError, ValueError, TypeError):
        return -1


def _end_process(process: Any) -> None:
    """Stop one CLI process tree through the platform facade; never raises."""
    if process is None or process.poll() is not None:
        return
    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and pid > 0:
        platform_io.terminate_process_tree(pid)
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            return
    _reap(process)


class _StderrTail:
    """Drain the child's stderr on its own thread, bounded to the last lines.

    A CLI that fills its stderr pipe while the turn is still reading stdout
    would otherwise block forever, so the drain never waits on the reader.
    """

    MAX_LINES = 40

    def __init__(self, stream: Any) -> None:
        self._lines: list[str] = []
        self._thread: threading.Thread | None = None
        if stream is None:
            return
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream: Any) -> None:
        try:
            for line in stream:
                self._lines.append(line)
                del self._lines[: -self.MAX_LINES]
        except (OSError, ValueError):
            return

    def text(self) -> str:
        if self._thread is not None:
            self._thread.join(timeout=REAP_TIMEOUT_SECONDS)
        return "".join(self._lines)[-MAX_STDERR_CHARS:]


class CliManagerBackend:
    """One manager conversation on one agentic CLI, one non-interactive turn at a time.

    ``start`` only records the brief: spawning is deferred to the first ``send``
    so a started-but-silent session costs nothing. Each ``send`` runs one turn
    and yields its events as the lines arrive; the first turn carries the brief
    with the message, and every later turn resumes the conversation id captured
    from the stream. A turn that outruns ``timeout_seconds`` has its process
    killed and reports exactly one ``error`` event, as does a non-zero exit, and
    the backend stays closeable either way.
    """

    def __init__(
        self,
        backend_id: str,
        model: str,
        repo: Path | str,
        *,
        mcp_config_path: Path | str | None = None,
        timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
        plan_builder: Callable[..., Any] = runtime_adapters.build_manager_command,
        spawn: Callable[[Sequence[str], str | None], Any] = _spawn_cli,
    ) -> None:
        if backend_id not in MANAGER_BACKEND_IDS:
            raise ManagerLoopError(f"manager_backend_unsupported:{backend_id}")
        if mcp_config_path is not None and backend_id not in _MCP_CONFIG_FLAGS:
            raise ManagerLoopError(f"manager_backend_mcp_config_unsupported:{backend_id}")
        if not float(timeout_seconds) > 0:
            raise ValueError("timeout_seconds must be positive")
        self.backend_id = backend_id
        self.model = model
        self.repo = Path(repo)
        self.mcp_config_path = mcp_config_path
        self.timeout_seconds = float(timeout_seconds)
        self._plan_builder = plan_builder
        self._spawn = spawn
        self._brief = ""
        self._conversation_id = ""
        self._process: Any = None
        self._slot = threading.Lock()

    @property
    def conversation_id(self) -> str:
        """The provider conversation later turns resume, or ``""`` before the first turn."""
        return self._conversation_id

    def is_running(self) -> bool:
        """Whether this backend currently owns a live CLI process."""
        process = self._process
        return process is not None and process.poll() is None

    def start(self, brief: str) -> str:
        """Record ``brief`` as the first turn's preamble; do not spawn anything yet."""
        self._brief = str(brief or "")
        return f"cli:{self.backend_id}:{self.model}"

    def send(self, message: str) -> Iterator[dict[str, Any]]:
        """Run one CLI turn for ``message`` and yield its events while it runs."""
        return self._turn(str(message))

    def close(self) -> None:
        """End the turn's process if one is alive; safe to call any number of times."""
        with self._slot:
            process, self._process = self._process, None
        _end_process(process)

    def argv_for(self, plan: Any) -> list[str]:
        """This turn's argv: the adapter plan, minus what a manager turn cannot keep."""
        dropped = _DROPPED_TOKENS.get(self.backend_id, frozenset())
        argv = [token for token in plan.argv if token not in dropped]
        flag = _MCP_CONFIG_FLAGS.get(self.backend_id)
        if flag is not None and self.mcp_config_path is not None:
            argv.extend((flag, str(self.mcp_config_path)))
        return resume_argv(self.backend_id, argv, self._conversation_id)

    def _turn(self, message: str) -> Iterator[dict[str, Any]]:
        resuming = bool(self._conversation_id)
        prompt = message if resuming else f"{self._brief}\n\n{message}".strip()
        plan = self._plan_builder(self.backend_id, prompt, self.repo, model=self.model)
        if not getattr(plan, "launchable", False) or not plan.argv:
            yield _turn_error("launch_plan", plan.validation_reason or "plan_not_launchable")
            return
        try:
            process = self._spawn(self.argv_for(plan), plan.cwd)
        except OSError as exc:
            yield _turn_error("spawn", f"{type(exc).__name__}: {exc}")
            return
        with self._slot:
            self._process = process
        expired = threading.Event()
        tail = _StderrTail(getattr(process, "stderr", None))

        def cut_off() -> None:
            expired.set()
            _end_process(process)

        watchdog = threading.Timer(self.timeout_seconds, cut_off)
        watchdog.start()
        try:
            for raw in process.stdout or ():
                event = _decode(raw)
                if event is None:
                    continue
                self._conversation_id = self._conversation_id or conversation_id_of(event)
                yield from translate(self.backend_id, event)
        finally:
            watchdog.cancel()
            code = _reap(process)
            stderr = tail.text()
            with self._slot:
                if self._process is process:
                    self._process = None
        if expired.is_set():
            yield _turn_error("timeout", f"manager_turn_timeout_seconds={self.timeout_seconds:g}")
        elif code != 0:
            outcome = runtime_adapters.classify_provider_outcome(exit_code=code, stderr=stderr)
            yield _turn_error(
                str(outcome.get("outcome") or "worker_failed"),
                str(outcome.get("reason") or f"exit_code={code}"),
            )


def manager_backend_factory(
    repo: Path | str,
    *,
    mcp_config_path: Path | str | None = None,
    declares_route: Callable[..., bool] = workforce_catalog.catalog_declares_route,
    **options: Any,
) -> Callable[[str, str], CliManagerBackend]:
    """The ``backend_factory`` :class:`~aiworkhub.manager_loop.ManagerOrchestrator` takes.

    ``backend_id`` is resolved against the three manager CLIs and then against
    the repository's existing workforce registry, so a backend the catalog does
    not declare for this model is refused rather than launched. Every refusal is
    a named :class:`~aiworkhub.manager_loop.ManagerLoopError`.
    """

    root = Path(repo)

    def build(backend_id: str, model: str) -> CliManagerBackend:
        if backend_id not in MANAGER_BACKEND_IDS:
            raise ManagerLoopError(f"manager_backend_unsupported:{backend_id}")
        if not declares_route(root, backend_id, model):
            raise ManagerLoopError(f"manager_backend_unavailable:{backend_id}:{model}")
        return CliManagerBackend(
            backend_id, model, root, mcp_config_path=mcp_config_path, **options
        )

    return build
