"""Pure runtime command planning for supported local task adapters.

The plans produced here are inert data.  They contain an argument vector and
working directory for a launcher to use, but this module never starts a child
process and never accepts or returns an environment mapping.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any


SUPPORTED_ADAPTERS: tuple[str, ...] = (
    "vscode_lm",
    "claude_cli",
    "codex_cli",
    "deepseek_copilot_cli",
    "deepseek_vscode_lm",
    "glm_copilot_cli",
    "glm_vscode_lm",
    "deepseek_manual",
)
LOCAL_ADAPTERS: tuple[str, ...] = (
    "vscode_lm",
    "claude_cli",
    "codex_cli",
    "deepseek_copilot_cli",
    "deepseek_vscode_lm",
    "glm_copilot_cli",
    "glm_vscode_lm",
)
MANUAL_ONLY_ADAPTERS: tuple[str, ...] = ("deepseek_manual",)

ADAPTER_EXECUTABLES: Mapping[str, str] = MappingProxyType(
    {
        "vscode_lm": sys.executable,
        "claude_cli": "claude",
        "codex_cli": "codex",
        # Official GitHub Copilot CLI, driven in BYOK mode against DeepSeek's
        # OpenAI-compatible API (see deepseek_credentials.py).
        "deepseek_copilot_cli": "copilot",
        "deepseek_vscode_lm": sys.executable,
        # Official GitHub Copilot CLI, driven in BYOK mode against GLM's
        # OpenAI-compatible API (see glm_credentials.py).
        "glm_copilot_cli": "copilot",
        "glm_vscode_lm": sys.executable,
    }
)

# DeepSeek model selection. ``pro`` is the production coding default; ``flash``
# is the cheaper/faster variant. A DeepSeek-labeled task may use only these
# models -- never a GitHub-hosted Claude/GPT model.
DEEPSEEK_COPILOT_ADAPTER = "deepseek_copilot_cli"
DEEPSEEK_VSCODE_LM_ADAPTER = "deepseek_vscode_lm"
DEEPSEEK_SUPPORTED_MODELS: tuple[str, ...] = ("deepseek-v4-pro", "deepseek-v4-flash")
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-pro"
# Copilot BYOK env var whose value the launcher declares as secret-redacted via
# the CLI's ``--secret-env-vars`` flag; the key is never placed in argv.
DEEPSEEK_SECRET_ENV_VAR = "COPILOT_PROVIDER_API_KEY"

GLM_COPILOT_ADAPTER = "glm_copilot_cli"
GLM_VSCODE_LM_ADAPTER = "glm_vscode_lm"
GLM_SUPPORTED_MODELS: tuple[str, ...] = ("glm-5.2",)
GLM_DEFAULT_MODEL = "glm-5.2"
GLM_SECRET_ENV_VAR = "COPILOT_PROVIDER_API_KEY"
VSCODE_LM_ADAPTER = "vscode_lm"
WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER = "windows_native_cli_requires_appcontainer_sandbox"

# Codex normally keeps its own workspace-write sandbox.  When Task MCP already
# places the whole worker under an outer Landlock/bubblewrap filesystem sandbox,
# the coordinator may disable only Codex's nested sandbox to avoid an unsupported
# bwrap-inside-Landlock network-namespace setup.  The safe default is unchanged.
CODEX_INNER_SANDBOX_MODE_ENV = "AIWORKHUB_CODEX_INNER_SANDBOX_MODE"
CODEX_INNER_SANDBOX_MODES: tuple[str, ...] = ("workspace-write", "danger-full-access")

# Autonomous workers discover code through the injected AIWorkHub Source
# Graph. These provider-native denies leave exact Read/Edit and card-declared
# validation available, but prevent raw repository search from silently
# replacing Source Graph. Unsupported/unindexed targets are handled by a new,
# exact coordinator-authorized fallback card rather than by weakening a live
# worker run.
CLAUDE_RAW_DISCOVERY_DENIES: tuple[str, ...] = (
    "Grep",
    "Glob",
    "Bash(grep *)",
    "Bash(rg *)",
    "Bash(find *)",
    "Bash(tree *)",
)
COPILOT_RAW_DISCOVERY_EXCLUDES = "grep,glob"
COPILOT_RAW_DISCOVERY_DENIES: tuple[str, ...] = (
    "shell(grep:*)",
    "shell(rg:*)",
    "shell(find:*)",
    "shell(tree:*)",
)

# ---------------------------------------------------------------------------
# Reviewer file-read capability.
#
# A quality reviewer receives its candidate as a review packet.  The vscode_lm
# routes run the reviewer model in-process inside the editor host and launch
# only a bounded response-applier subprocess, so the model is handed no
# file-read tool of any kind: it physically cannot open a packet delivered only
# as a filesystem path.  Such a reviewer is "blind" and needs the packet
# content delivered inline in its prompt instead (see quality_review).
#
# A Copilot CLI adapter normally does expose a file-read tool, but when the
# workforce records ``adapter_fallback_used`` the effective transport is the
# in-process vscode_lm bridge, so the reviewer is blind for this purpose too.
# ---------------------------------------------------------------------------

# Canonical worker file-read tool vocabulary, matched case-insensitively with
# ``-`` normalized to ``_``.  Kept identical to the provider read-event
# classifier so an offered capability set and an observed provider tool record
# agree on what counts as a file read.
WORKER_FILE_READ_TOOL_NAMES: frozenset[str] = frozenset(
    {"read", "read_file", "readfile", "file_read"}
)

# Adapters that run the reviewer model in-process and expose no file-read tool.
NO_FILE_READ_ADAPTERS: frozenset[str] = frozenset(
    {VSCODE_LM_ADAPTER, GLM_VSCODE_LM_ADAPTER, DEEPSEEK_VSCODE_LM_ADAPTER}
)

# Provider family for each supported adapter.  Independence is measured by
# provider (and then model), never by adapter route, so both GLM routes map to
# the same provider.  An unknown adapter maps to itself so it can never be
# silently treated as identical to a known provider.
_ADAPTER_PROVIDERS: Mapping[str, str] = MappingProxyType(
    {
        "vscode_lm": "vscode_lm",
        "claude_cli": "claude",
        "codex_cli": "gpt",
        "deepseek_copilot_cli": "deepseek",
        "deepseek_vscode_lm": "deepseek",
        "glm_copilot_cli": "glm",
        "glm_vscode_lm": "glm",
    }
)


def provider_for_adapter(adapter_id: str) -> str:
    """Return the provider family an adapter belongs to.

    Independence classification compares provider families, so both GLM routes
    (`glm_copilot_cli`, `glm_vscode_lm`) resolve to ``glm``.  An unknown adapter
    resolves to its own id so it is never conflated with a known provider.
    """

    return _ADAPTER_PROVIDERS.get(str(adapter_id), str(adapter_id))


def adapter_provides_file_read(
    adapter_id: str, *, adapter_fallback_used: bool = False
) -> bool:
    """Return True when a reviewer on this adapter has a worker file-read tool.

    In-process vscode_lm routes never do.  A Copilot CLI adapter that fell back
    to the in-process bridge (``adapter_fallback_used``) is treated as blind for
    the same reason.  This is a pure capability statement and starts no process.
    """

    if adapter_id not in SUPPORTED_ADAPTERS:
        return False
    if adapter_id in NO_FILE_READ_ADAPTERS:
        return False
    if adapter_fallback_used:
        return False
    return True


def capability_set_has_file_read(tool_names: Iterable[str]) -> bool:
    """Return True when a reviewer tool capability set includes a file read.

    ``tool_names`` is the exact set of tool names offered to the reviewer.
    Names are lower-cased with ``-`` normalized to ``_``; a name matches when it
    equals a known read tool or ends with one on an underscore boundary, so
    ``Read``, ``read_file`` and ``aiworkhub_worker_file_read`` all count while a
    submit or source-graph tool does not.
    """

    for name in tool_names:
        normalized = str(name).strip().lower().replace("-", "_")
        if normalized in WORKER_FILE_READ_TOOL_NAMES:
            return True
        for candidate in WORKER_FILE_READ_TOOL_NAMES:
            if normalized.endswith("_" + candidate):
                return True
    return False


PathValue = str | os.PathLike[str]
ExecutableOverrides = Mapping[str, PathValue]


@dataclass(frozen=True, slots=True)
class ExecutableResolution:
    """Result of resolving one adapter's local executable."""

    adapter_id: str
    executable: str | None
    ok: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "executable": self.executable,
            "ok": self.ok,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RuntimeAdapterPlan:
    """A validated, non-executing adapter command plan.

    ``argv`` is always a token list, never a shell command.  A manual-only
    plan is validation-successful but deliberately not launchable.
    """

    adapter_id: str
    argv: list[str]
    cwd: str | None
    executable: str | None
    launchable: bool
    manual_only: bool
    validation_ok: bool
    validation_reason: str

    @property
    def reason(self) -> str:
        """Short alias useful to callers rendering adapter status."""
        return self.validation_reason

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "executable": self.executable,
            "launchable": self.launchable,
            "manual_only": self.manual_only,
            "validation_ok": self.validation_ok,
            "validation_reason": self.validation_reason,
        }


def _invalid_plan(
    adapter_id: str,
    reason: str,
    *,
    cwd: str | None = None,
    manual_only: bool = False,
) -> RuntimeAdapterPlan:
    return RuntimeAdapterPlan(
        adapter_id=adapter_id,
        argv=[],
        cwd=cwd,
        executable=None,
        launchable=False,
        manual_only=manual_only,
        validation_ok=False,
        validation_reason=reason,
    )


def _validate_override_mapping(
    executable_overrides: ExecutableOverrides | None,
) -> str | None:
    if executable_overrides is None:
        return None
    if not isinstance(executable_overrides, Mapping):
        return "executable overrides must be a mapping keyed by adapter id"
    if any(key not in LOCAL_ADAPTERS for key in executable_overrides):
        return "executable overrides contain an unsupported adapter key"
    return None


def _resolve_override(adapter_id: str, raw_path: PathValue) -> ExecutableResolution:
    try:
        candidate = Path(raw_path)
    except (TypeError, ValueError):
        return ExecutableResolution(
            adapter_id, None, False, "executable override must be a filesystem path"
        )

    if not candidate.is_absolute():
        return ExecutableResolution(
            adapter_id, None, False, "executable override must be an absolute path"
        )

    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return ExecutableResolution(
            adapter_id, None, False, "executable override does not exist"
        )

    if not resolved.is_file():
        return ExecutableResolution(
            adapter_id, None, False, "executable override is not a file"
        )
    if not os.access(resolved, os.X_OK):
        return ExecutableResolution(
            adapter_id, None, False, "executable override is not executable"
        )

    return ExecutableResolution(adapter_id, str(resolved), True, "")


def resolve_executable(
    adapter_id: str,
    executable_overrides: ExecutableOverrides | None = None,
) -> ExecutableResolution:
    """Resolve a supported adapter executable without executing it.

    Caller-provided overrides take precedence over ``shutil.which``.  Override
    values are accepted only for local adapters and only as absolute paths to
    executable regular files.
    """

    if adapter_id not in SUPPORTED_ADAPTERS:
        return ExecutableResolution(
            adapter_id, None, False, "unsupported adapter"
        )

    override_error = _validate_override_mapping(executable_overrides)
    if override_error:
        return ExecutableResolution(adapter_id, None, False, override_error)

    if adapter_id in MANUAL_ONLY_ADAPTERS:
        return ExecutableResolution(
            adapter_id,
            None,
            False,
            "manual-only adapter has no local executable",
        )

    if executable_overrides is not None and adapter_id in executable_overrides:
        return _resolve_override(adapter_id, executable_overrides[adapter_id])

    binary = ADAPTER_EXECUTABLES[adapter_id]
    discovered = shutil.which(binary)
    if not discovered:
        return ExecutableResolution(
            adapter_id, None, False, f"executable not found: {binary}"
        )

    try:
        resolved = Path(discovered).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return ExecutableResolution(
            adapter_id, None, False, f"discovered executable is not a file: {binary}"
        )

    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return ExecutableResolution(
            adapter_id, None, False, f"discovered executable is not executable: {binary}"
        )

    return ExecutableResolution(adapter_id, str(resolved), True, "")


def resolve_deepseek_model(model: str | None) -> tuple[str | None, str | None]:
    """Resolve and guard a DeepSeek model selection.

    Returns ``(resolved_model, None)`` for a supported model, defaulting a
    missing selection to production ``deepseek-v4-pro``. Returns
    ``(None, reason)`` for any non-DeepSeek model so a DeepSeek-labeled task can
    never be routed to a GitHub-hosted Claude/GPT model.
    """
    if model is None:
        return DEEPSEEK_DEFAULT_MODEL, None
    if not isinstance(model, str) or not model.strip():
        return None, "model must be a nonempty string when provided"
    if "\x00" in model:
        return None, "model contains a NUL character"
    candidate = model.strip()
    if candidate not in DEEPSEEK_SUPPORTED_MODELS:
        return None, (
            "unsupported_deepseek_model:"
            f"{candidate}:allowed={'|'.join(DEEPSEEK_SUPPORTED_MODELS)}"
        )
    return candidate, None


def resolve_glm_model(model: str | None) -> tuple[str | None, str | None]:
    """Resolve and guard a GLM model selection.

    Only the explicit production model ``glm-5.2`` is supported.  A GLM-labeled
    task must never fall through to a GitHub-hosted Claude/GPT model or to a
    different GLM family.
    """
    if model is None:
        return GLM_DEFAULT_MODEL, None
    if not isinstance(model, str) or not model.strip():
        return None, "model must be a nonempty string when provided"
    if "\x00" in model:
        return None, "model contains a NUL character"
    candidate = model.strip()
    if candidate not in GLM_SUPPORTED_MODELS:
        return None, (
            "unsupported_glm_model:"
            f"{candidate}:allowed={'|'.join(GLM_SUPPORTED_MODELS)}"
        )
    return candidate, None


def _resolve_repo(repo: PathValue) -> tuple[str | None, str | None]:
    if isinstance(repo, str) and not repo.strip():
        return None, "repo path must be nonempty"

    try:
        resolved = Path(repo).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None, "repo path does not exist or cannot be resolved"

    if not resolved.is_dir():
        return None, "repo path is not a directory"
    return str(resolved), None


def _is_windows_host() -> bool:
    return os.name == "nt"


def _resolve_additional_readonly_dirs(
    values: Sequence[PathValue] | None,
) -> tuple[list[str], str | None]:
    """Resolve coordinator-approved Copilot read directories.

    The repository launcher owns the allowlist decision; this adapter layer
    only enforces an absolute, existing-directory argv shape and deduplicates
    it.  It never broadens a file path to a directory itself.
    """
    if values is None:
        return [], None
    if isinstance(values, (str, bytes, os.PathLike)):
        return [], "additional readonly dirs must be a sequence of paths"
    resolved: list[str] = []
    try:
        candidates = list(values)
    except TypeError:
        return [], "additional readonly dirs must be a sequence of paths"
    for raw in candidates:
        try:
            candidate = Path(raw)
        except (TypeError, ValueError):
            return [], "additional readonly dir must be a filesystem path"
        if not candidate.is_absolute():
            return [], "additional readonly dir must be absolute"
        try:
            directory = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return [], "additional readonly dir does not exist"
        if not directory.is_dir():
            return [], "additional readonly path is not a directory"
        value = str(directory)
        if value not in resolved:
            resolved.append(value)
    return resolved, None


def build_runtime_command(
    adapter_id: str,
    prompt: str,
    repo: PathValue,
    *,
    model: str | None = None,
    executable_overrides: ExecutableOverrides | None = None,
    outer_sandbox_backend: str | None = None,
    additional_readonly_dirs: Sequence[PathValue] | None = None,
    include_partial_messages: bool = False,
) -> RuntimeAdapterPlan:
    """Build a validated argv/cwd plan for one supported adapter.

    Invalid input and unavailable executables produce non-launchable plans
    with empty argv.  Prompt and model strings are preserved as single argv
    tokens, including spaces and Unicode text.
    """

    if not isinstance(adapter_id, str) or adapter_id not in SUPPORTED_ADAPTERS:
        safe_adapter_id = adapter_id if isinstance(adapter_id, str) else ""
        return _invalid_plan(safe_adapter_id, "unsupported adapter")

    if not isinstance(prompt, str) or not prompt.strip():
        return _invalid_plan(adapter_id, "prompt must be a nonempty string")
    if "\x00" in prompt:
        return _invalid_plan(adapter_id, "prompt contains a NUL character")

    if model is not None:
        if not isinstance(model, str) or not model.strip():
            return _invalid_plan(adapter_id, "model must be a nonempty string when provided")
        if "\x00" in model:
            return _invalid_plan(adapter_id, "model contains a NUL character")

    cwd, repo_error = _resolve_repo(repo)
    if repo_error:
        return _invalid_plan(adapter_id, repo_error)
    assert cwd is not None

    readonly_dirs, readonly_error = _resolve_additional_readonly_dirs(
        additional_readonly_dirs
    )
    if readonly_error:
        return _invalid_plan(adapter_id, readonly_error, cwd=cwd)
    if readonly_dirs and adapter_id not in {DEEPSEEK_COPILOT_ADAPTER, GLM_COPILOT_ADAPTER}:
        return _invalid_plan(
            adapter_id,
            "additional readonly dirs are supported only by deepseek_copilot_cli",
            cwd=cwd,
        )

    override_error = _validate_override_mapping(executable_overrides)
    if override_error:
        return _invalid_plan(
            adapter_id,
            override_error,
            cwd=cwd,
            manual_only=adapter_id in MANUAL_ONLY_ADAPTERS,
        )

    if adapter_id in MANUAL_ONLY_ADAPTERS:
        return RuntimeAdapterPlan(
            adapter_id=adapter_id,
            argv=[],
            cwd=cwd,
            executable=None,
            launchable=False,
            manual_only=True,
            validation_ok=True,
            validation_reason="manual-only adapter; no local command is available",
        )

    resolution = resolve_executable(adapter_id, executable_overrides)
    if not resolution.ok or resolution.executable is None:
        return _invalid_plan(adapter_id, resolution.reason, cwd=cwd)

    executable = resolution.executable
    if adapter_id in {VSCODE_LM_ADAPTER, GLM_VSCODE_LM_ADAPTER, DEEPSEEK_VSCODE_LM_ADAPTER}:
        return _invalid_plan(
            adapter_id,
            "vscode_lm_requires_process_launcher_bridge_context",
            cwd=cwd,
        )
    if _is_windows_host() and outer_sandbox_backend != "appcontainer":
        return _invalid_plan(
            adapter_id,
            WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER,
            cwd=cwd,
        )
    if adapter_id == "claude_cli":
        argv = [
            executable,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            "Read",
            "Write",
            "Edit",
            "Bash",
            "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_source_graph_query",
            "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_semantic_edit_prepare",
            "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_semantic_edit_apply",
            "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_session_current_state",
            "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_ai_memory_search",
            "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_ai_memory_get",
            "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_ai_memory_related",
            "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_kb_search",
            "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_kb_get",
            "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_kb_related",
            "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_session_write_intent",
            "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_ai_memory_write_intent",
            "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_kb_write_intent",
            "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_quality_review_submit",
            "--no-session-persistence",
            "--disallowedTools",
            *CLAUDE_RAW_DISCOVERY_DENIES,
        ]
        # Partial-message mode emits a JSON event for nearly every provider
        # delta and can turn a small task into a multi-megabyte stdout log.
        # Terminal stream-json messages retain final text, tool receipts,
        # usage and exit evidence. Enable deltas only when an explicit owner
        # token budget needs live cumulative-usage enforcement.
        if include_partial_messages:
            argv.insert(argv.index("--permission-mode"), "--include-partial-messages")
        if model is not None:
            argv.extend(("--model", model))
    elif adapter_id == "codex_cli":
        if outer_sandbox_backend in {"landlock", "bubblewrap"}:
            codex_sandbox_mode = "danger-full-access"
        else:
            codex_sandbox_mode = os.environ.get(
                CODEX_INNER_SANDBOX_MODE_ENV,
                "workspace-write",
            )
        if codex_sandbox_mode not in CODEX_INNER_SANDBOX_MODES:
            return _invalid_plan(
                adapter_id,
                f"invalid Codex inner sandbox mode: {codex_sandbox_mode}",
                cwd=cwd,
            )
        argv = [
            executable,
            "exec",
            "--json",
            "--ephemeral",
            "-s",
            codex_sandbox_mode,
            "-C",
            cwd,
        ]
        if model is not None:
            argv.extend(("--model", model))
        argv.append(prompt)
    else:  # Copilot CLI in BYOK mode for OpenAI-compatible local-worker adapters
        if adapter_id == DEEPSEEK_COPILOT_ADAPTER:
            resolved_model, model_error = resolve_deepseek_model(model)
            secret_env_var = DEEPSEEK_SECRET_ENV_VAR
        else:
            resolved_model, model_error = resolve_glm_model(model)
            secret_env_var = GLM_SECRET_ENV_VAR
        if model_error:
            return _invalid_plan(adapter_id, model_error, cwd=cwd)
        assert resolved_model is not None
        # One prompt token (-p), JSONL output, autonomous/no-question mode
        # (--allow-all-tools required for non-interactive + --no-ask-user),
        # disabled remote export/control and unneeded built-in MCPs, explicit
        # cwd/model, and a declared secret-redaction of the BYOK API key. The
        # API key itself is NEVER in argv; it enters only the child env. We do
        # NOT pass --allow-all-paths/--yolo, so Copilot's own permissions stay
        # subordinate to the outer Landlock/bubblewrap filesystem sandbox.
        argv = [
            executable,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--allow-all-tools",
            f"--excluded-tools={COPILOT_RAW_DISCOVERY_EXCLUDES}",
            "--no-ask-user",
            "--no-remote",
            "--no-remote-export",
            "--disable-builtin-mcps",
            "--no-color",
            "--no-auto-update",
            "--secret-env-vars",
            secret_env_var,
        ]
        for denied_tool in COPILOT_RAW_DISCOVERY_DENIES:
            argv.append(f"--deny-tool={denied_tool}")
        for directory in readonly_dirs:
            argv.extend(("--add-dir", directory))
        argv.extend(("-C", cwd, "--model", resolved_model))

    return RuntimeAdapterPlan(
        adapter_id=adapter_id,
        argv=argv,
        cwd=cwd,
        executable=executable,
        launchable=True,
        manual_only=False,
        validation_ok=True,
        validation_reason="",
    )


def inject_worker_mcp_config(
    plan: RuntimeAdapterPlan,
    worker_mcp_config_path: PathValue,
) -> RuntimeAdapterPlan:
    """Return a new plan with the B833 worker MCP config flag appended.

    ``claude_cli`` receives ``--mcp-config <path> --strict-mcp-config``;
    ``deepseek_copilot_cli``/``glm_copilot_cli`` receive
    ``--additional-mcp-config <path>``. ``codex_cli`` is unchanged here --
    Codex instead picks up its worker MCP server from the isolated
    ``$HOME/.codex/config.toml`` the launcher provisions alongside this
    config (see ``worker_ai_tools_mcp.generate_worker_mcp_runtime``).

    A non-launchable plan or a manual-only plan is returned UNCHANGED -- it
    was never going to receive the flag regardless of MCP provisioning.

    For an adapter that DOES require the worker MCP surface (``claude_cli``,
    ``deepseek_copilot_cli``, ``glm_copilot_cli``), a config path that fails
    to resolve to an existing, non-symlink file raises ``ValueError`` instead
    of silently returning the plan unchanged (B834 repair: the B833 candidate
    degraded to "launch without worker tools" here, which is exactly the
    silent-degradation failure mode the generated-config-is-mandatory
    requirement forbids -- a provisioning problem must reject the launch).
    """

    if not plan.launchable or plan.manual_only:
        return plan
    if plan.adapter_id not in {"claude_cli", DEEPSEEK_COPILOT_ADAPTER, GLM_COPILOT_ADAPTER}:
        return plan
    try:
        resolved = Path(worker_mcp_config_path).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError(f"worker_mcp_config_unresolvable:{worker_mcp_config_path}:{exc}") from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError(f"worker_mcp_config_missing_or_not_a_file:{resolved}")

    if plan.adapter_id == "claude_cli":
        argv = [*plan.argv, "--mcp-config", str(resolved), "--strict-mcp-config"]
    else:
        argv = [*plan.argv, "--additional-mcp-config", f"@{resolved}"]

    return RuntimeAdapterPlan(
        adapter_id=plan.adapter_id,
        argv=argv,
        cwd=plan.cwd,
        executable=plan.executable,
        launchable=plan.launchable,
        manual_only=plan.manual_only,
        validation_ok=plan.validation_ok,
        validation_reason=plan.validation_reason,
    )


def build_adapter_command(
    adapter_id: str,
    prompt: str,
    repo: PathValue,
    *,
    model: str | None = None,
    executable_overrides: ExecutableOverrides | None = None,
    outer_sandbox_backend: str | None = None,
    additional_readonly_dirs: Sequence[PathValue] | None = None,
    include_partial_messages: bool = False,
) -> RuntimeAdapterPlan:
    """Compatibility name for callers that describe commands by adapter."""

    return build_runtime_command(
        adapter_id,
        prompt,
        repo,
        model=model,
        executable_overrides=executable_overrides,
        outer_sandbox_backend=outer_sandbox_backend,
        additional_readonly_dirs=additional_readonly_dirs,
        include_partial_messages=include_partial_messages,
    )


__all__ = [
    "ADAPTER_EXECUTABLES",
    "DEEPSEEK_COPILOT_ADAPTER",
    "DEEPSEEK_VSCODE_LM_ADAPTER",
    "DEEPSEEK_DEFAULT_MODEL",
    "DEEPSEEK_SECRET_ENV_VAR",
    "DEEPSEEK_SUPPORTED_MODELS",
    "GLM_COPILOT_ADAPTER",
    "GLM_VSCODE_LM_ADAPTER",
    "GLM_DEFAULT_MODEL",
    "GLM_SECRET_ENV_VAR",
    "GLM_SUPPORTED_MODELS",
    "LOCAL_ADAPTERS",
    "MANUAL_ONLY_ADAPTERS",
    "SUPPORTED_ADAPTERS",
    "VSCODE_LM_ADAPTER",
    "ExecutableResolution",
    "RuntimeAdapterPlan",
    "build_adapter_command",
    "build_runtime_command",
    "inject_worker_mcp_config",
    "resolve_deepseek_model",
    "resolve_executable",
    "resolve_glm_model",
]
