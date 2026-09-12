"""Pure runtime command planning for supported local task adapters.

The plans produced here are inert data.  They contain an argument vector and
working directory for a launcher to use, but this module never starts a child
process and never accepts or returns an environment mapping.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import platform_io


SUPPORTED_ADAPTERS: tuple[str, ...] = (
    "vscode_lm",
    "claude_cli",
    "codex_cli",
    "deepseek_copilot_cli",
    "deepseek_vscode_lm",
    "glm_copilot_cli",
    "glm_vscode_lm",
    "grok_kilo_cli",
    "opencode_cli",
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
    "grok_kilo_cli",
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
        # Official Kilo CLI, authenticated against xAI in a request-scoped
        # home by the launcher.  This pure planner never reads credentials.
        "grok_kilo_cli": "kilo",
        "opencode_cli": "opencode",
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
# GLM_SUPPORTED_MODELS is the Copilot BYOK *credential* surface for the
# open.bigmodel.cn endpoint (see glm_credentials.py); it is NOT the editor's
# callable catalog. The set of GLM models AIWorkHub can drive through the
# editor is discovered from vscode.lm at runtime (see the editor model
# vocabulary below), never enumerated here.
GLM_SUPPORTED_MODELS: tuple[str, ...] = ("glm-5.2",)
GLM_DEFAULT_MODEL = "glm-5.2"
GLM_SECRET_ENV_VAR = "COPILOT_PROVIDER_API_KEY"
GROK_KILO_ADAPTER = "grok_kilo_cli"
GROK_KILO_SUPPORTED_MODELS: tuple[str, ...] = ("xai/grok-4.6",)
GROK_KILO_DEFAULT_MODEL = GROK_KILO_SUPPORTED_MODELS[0]
_KILO_EXTENSION_DIR_GLOB = "kilocode.kilo-code-*"
OPENCODE_CLI_ADAPTER = "opencode_cli"
OPENCODE_SNAP_BIN = "/snap/bin/opencode"
OPENCODE_WORKER_MCP_SERVER = "aiworkhub_worker_ai_tools"
OPENCODE_PERMISSION_ALLOW = "allow"
OPENCODE_PERMISSION_DENY = "deny"
OPENCODE_WINDOWS_RESOLUTION_FAIL_CLOSED = "opencode_cli_windows_resolution_fail_closed"
VSCODE_LM_ADAPTER = "vscode_lm"
WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER = "windows_native_cli_requires_appcontainer_sandbox"
# The single spelling of the Windows confinement backend.  This exact string is
# what worker_supervisor dispatches its AppContainer launch on
# (``execution_backend == "windows_appcontainer"``) and what repo_policy gates
# native-CLI launchability on.  Accepting any other spelling here would hand
# back a plan that READS as sandboxed while the supervisor falls through to its
# plain ``subprocess.Popen`` branch -- a worker held only by a kill-on-close Job
# Object, with no filesystem, registry or network boundary whatsoever.  One
# identifier, shared by planner, policy and supervisor, is what makes that
# mismatch impossible.
WINDOWS_APPCONTAINER_SANDBOX_BACKEND = "windows_appcontainer"

# ── Route families ─────────────────────────────────────────────────────────
# A route family is the PROTOCOL a route speaks, not the model behind it.
# ``glm_vscode_lm`` and ``deepseek_vscode_lm`` are two models over one editor
# bridge: they share a transport, a tool-dispatch allowlist and an event
# envelope, so every capability statement true of one is true of the other.
# Keying capability contracts on this value -- rather than on an adapter or
# model name -- is what lets a newly added model on an existing family inherit
# that family's verified contract without a code change
# (see provider_route_contracts.py).
ROUTE_FAMILY_EDITOR_VSCODE_LM = "editor_hosted_vscode_lm"
ROUTE_FAMILY_COPILOT_BYOK_CLI = "copilot_byok_cli"
ROUTE_FAMILY_CLAUDE_CLI = "claude_cli"
ROUTE_FAMILY_CODEX_CLI = "codex_cli"
ROUTE_FAMILY_KILO_XAI_CLI = "kilo_xai_cli"
ROUTE_FAMILY_OPENCODE_CLI = "opencode_cli"
ROUTE_FAMILY_UNKNOWN = "unknown"

_ROUTE_FAMILY_BY_ADAPTER: Mapping[str, str] = MappingProxyType(
    {
        VSCODE_LM_ADAPTER: ROUTE_FAMILY_EDITOR_VSCODE_LM,
        GLM_VSCODE_LM_ADAPTER: ROUTE_FAMILY_EDITOR_VSCODE_LM,
        DEEPSEEK_VSCODE_LM_ADAPTER: ROUTE_FAMILY_EDITOR_VSCODE_LM,
        DEEPSEEK_COPILOT_ADAPTER: ROUTE_FAMILY_COPILOT_BYOK_CLI,
        GLM_COPILOT_ADAPTER: ROUTE_FAMILY_COPILOT_BYOK_CLI,
        "claude_cli": ROUTE_FAMILY_CLAUDE_CLI,
        "codex_cli": ROUTE_FAMILY_CODEX_CLI,
        GROK_KILO_ADAPTER: ROUTE_FAMILY_KILO_XAI_CLI,
        OPENCODE_CLI_ADAPTER: ROUTE_FAMILY_OPENCODE_CLI,
    }
)

def route_family(adapter_id: str) -> str:
    """Return the protocol family this adapter speaks, or ``unknown``.

    An unrecognized adapter resolves to ``unknown`` rather than raising or
    guessing: callers fail closed on that value instead of letting a new
    adapter silently inherit some other family's capability contract.
    """

    if not isinstance(adapter_id, str):
        return ROUTE_FAMILY_UNKNOWN
    return _ROUTE_FAMILY_BY_ADAPTER.get(adapter_id, ROUTE_FAMILY_UNKNOWN)

# ── Worker temp authority environment variables ────────────────────────────
# THE single declaration of which environment variables carry a launched
# worker's temporary-file root.  Both real ProcessManager launch paths overlay
# exactly these keys with the request-owned repository-local temp authority
# (``.aiworkhub/temp/worker/<request_id>/tmp``) so a worker-run pytest/tempfile
# never lands in the shared system temp or inside the candidate worktree.  This
# is inert naming data only: consistent with this module's contract it never
# builds, mutates, or returns an environment mapping -- it just names the keys
# ``process_launcher.worker_launch_env`` and ``worker_workspace`` agree on
# (POSIX ``TMPDIR`` plus the ``TMP``/``TEMP`` names Windows and macOS honour).
WORKER_TEMP_ENV_VARS: tuple[str, ...] = ("TMPDIR", "TMP", "TEMP")

# ── Editor model vocabulary ────────────────────────────────────────────────
# THE single declaration of how a VS Code-reported model is judged callable.
# vscode_lm_bridge, workforce_catalog and the VS Code extension all consume
# these rules; tests/test_lm_model_discovery.py fails if any consumer drifts
# (the extension mirror is checked against these literals).
#
# This is deliberately NOT an enumeration of model names. The callable model
# names are read from ``vscode.lm.selectChatModels`` at runtime and surfaced
# into the config from that discovery -- never typed by hand. The only GLM name
# in this module is the SINGLE cold-start fallback below, which a running system
# never consults once a live editor host has reported a catalog.
#
# The non-callable filters below are measured exclusions, not an allowlist of
# names: ``copilotcli``/``claude-code`` picker entries return empty streams when
# invoked through the public LM API, and ``copilot-utility*`` entries have no
# tokenizer (``Unknown tokenizer: undefined`` on sendRequest).
EDITOR_NONCALLABLE_VENDORS: frozenset[str] = frozenset({"copilotcli", "claude-code"})
EDITOR_NONCALLABLE_ID_PREFIXES: tuple[str, ...] = ("copilot-utility",)
EDITOR_REQUESTED_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}$")
# Cold-start ONLY: used when no editor host has reported a callable catalog yet
# so a first launch is not dead. A running system consults the discovered
# catalog (bridge readiness / workforce catalog), never this constant.
GLM_COLD_START_FALLBACK_MODEL = "glm-5.2"


def _normalize_editor_token(value: Any) -> str:
    """Lower-case and fold non-alphanumeric runs to ``-``.

    Mirrors the extension's ``normalizedVscodeLmModelName`` so both sides agree
    on model identity when applying the non-callable filters.
    """

    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")


def editor_requested_name_ok(name: Any) -> bool:
    """True when ``name`` is a well-formed requested/reported model id."""

    return bool(EDITOR_REQUESTED_MODEL_RE.fullmatch(str(name or "").strip()))


def editor_model_is_callable(
    *, vendor: Any = "", model_id: Any = "", family: Any = "",
) -> bool:
    """Apply the measured non-callable provider filters to one reported model.

    A measured exclusion, never an allowlist of names: an excluded entry is one
    that demonstrably returns empty streams or has no tokenizer.
    """

    if _normalize_editor_token(vendor) in EDITOR_NONCALLABLE_VENDORS:
        return False
    id_n = _normalize_editor_token(model_id)
    family_n = _normalize_editor_token(family)
    return not any(
        id_n.startswith(prefix) or family_n.startswith(prefix)
        for prefix in EDITOR_NONCALLABLE_ID_PREFIXES
    )


def discover_callable_model_names(
    reported: Iterable[Mapping[str, Any]],
) -> list[str]:
    """Return the de-duplicated callable model names from a raw editor report.

    ``reported`` is the ``vscode.lm.selectChatModels`` result (each entry an
    id/family/vendor mapping). Non-callable providers are excluded and only
    regex-valid ids and families survive. No name is enumerated here: the set is
    exactly what the editor reported minus what measurement excludes.
    """

    names: list[str] = []
    for entry in reported:
        if not isinstance(entry, Mapping):
            continue
        if not editor_model_is_callable(
            vendor=entry.get("vendor"),
            model_id=entry.get("id"),
            family=entry.get("family"),
        ):
            continue
        for field in ("id", "family"):
            raw = str(entry.get(field) or "").strip()
            if raw and editor_requested_name_ok(raw) and raw not in names:
                names.append(raw)
    return names

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
#
# THE single raw-discovery command vocabulary.  Every provider tuple below is
# derived from it, so a command is added or removed in exactly one place.  It is
# deliberately identical to ``repo_policy.MANDATORY_RAW_DISCOVERY_DENIES`` and
# ``tests/test_runtime_adapters.py`` fails if the two ever drift.  That module
# cannot be imported here to share the constant: ``repo_policy`` already imports
# this one, so the dependency may only ever run in that direction.
RAW_DISCOVERY_DENIED_COMMANDS: tuple[str, ...] = ("grep", "rg", "find", "tree")

# Native provider search tools, denied alongside the shell commands for BUILD
# workers: Source Graph is the discovery path a worker is held to, and the
# repository policy names it.  Claude exposes them as ``Grep``/``Glob`` tool
# names; Copilot takes lower-case ``grep``/``glob`` entries for
# ``--excluded-tools``.  A read-only reviewer is the measured exception -- see
# ``CLAUDE_REVIEWER_SEARCH_TOOLS`` -- and receives only the shell denies.
CLAUDE_RAW_DISCOVERY_TOOL_DENIES: tuple[str, ...] = ("Grep", "Glob")
COPILOT_RAW_DISCOVERY_EXCLUDED_TOOLS: tuple[str, ...] = ("grep", "glob")

CLAUDE_RAW_DISCOVERY_SHELL_DENIES: tuple[str, ...] = tuple(
    f"Bash({command} *)" for command in RAW_DISCOVERY_DENIED_COMMANDS
)
CLAUDE_RAW_DISCOVERY_DENIES: tuple[str, ...] = (
    CLAUDE_RAW_DISCOVERY_TOOL_DENIES + CLAUDE_RAW_DISCOVERY_SHELL_DENIES
)
COPILOT_RAW_DISCOVERY_EXCLUDES = ",".join(COPILOT_RAW_DISCOVERY_EXCLUDED_TOOLS)
COPILOT_RAW_DISCOVERY_DENIES: tuple[str, ...] = tuple(
    f"shell({command}:*)" for command in RAW_DISCOVERY_DENIED_COMMANDS
)

# ---------------------------------------------------------------------------
# Raw-discovery deny ENFORCEMENT capability.
#
# Denying raw search is a provider-CLI argv capability, not a policy wish.  A
# flag that does not exist cannot be passed, and a flag that is accepted and
# then silently discarded is worse than no flag at all, because every
# downstream reader treats the unenforced run as an enforced one.  So this
# records what each transport can actually do, and ``build_runtime_command``
# passes a deny only on the branches where that is true.
#
# Verified against the installed CLIs on 2026-09-07:
#   claude_cli     ``claude --help`` lists
#                  ``--disallowedTools, --disallowed-tools <tools...>``.
#   *_copilot_cli  ``copilot --help`` lists ``--deny-tool[=tools...]`` and
#                  ``--excluded-tools[=tools...]``.
#   codex_cli      ``codex exec --help`` lists NO tool-deny option of any kind,
#                  and ``codex debug prompt-input -c <unknown-key>=1`` exits 0,
#                  so a speculative ``-c`` override would be accepted and
#                  silently ignored rather than enforced.
#   grok_kilo_cli  ``kilo run --help`` lists NO tool-deny option.  Its
#                  ``--auto`` flag auto-approves "permissions that are not
#                  explicitly denied", and that denial set is on-disk config.
#   *_vscode_lm    builds no provider argv at all (the in-process bridge).
#
# Codex and Kilo can each still be constrained by an on-disk policy file (Codex
# execpolicy ``.rules``, loaded unless ``--ignore-rules``; Kilo's config
# permission block).  Neither is an argv concern and neither is provisioned by
# this module, so neither is claimed here.  Routing should prefer an enforcing
# transport for code cards until a launcher provisions such a file.
#
# The polarity is inverted relative to ``NO_FILE_READ_ADAPTERS`` on purpose: an
# unlisted or unknown adapter must default to "does not enforce", so a newly
# added transport can never inherit an enforcement claim nobody verified.
# ---------------------------------------------------------------------------
RAW_DISCOVERY_ENFORCING_ADAPTERS: frozenset[str] = frozenset(
    {"claude_cli", DEEPSEEK_COPILOT_ADAPTER, GLM_COPILOT_ADAPTER}
)
RAW_DISCOVERY_ENFORCEMENT_UNVERIFIED = "adapter not verified for a tool-deny flag"
_RAW_DISCOVERY_ENFORCEMENT_REASONS: Mapping[str, str] = MappingProxyType(
    {
        "claude_cli": "argv --disallowedTools",
        DEEPSEEK_COPILOT_ADAPTER: "argv --excluded-tools and --deny-tool",
        GLM_COPILOT_ADAPTER: "argv --excluded-tools and --deny-tool",
        "codex_cli": (
            "codex exec exposes no tool-deny flag and accepts unknown -c "
            "overrides silently"
        ),
        GROK_KILO_ADAPTER: "kilo run exposes no tool-deny flag",
        OPENCODE_CLI_ADAPTER: (
            "opencode run exposes no argv tool-deny flag; "
            "permission deny is request-local config"
        ),
        VSCODE_LM_ADAPTER: "in-process bridge builds no provider argv",
        GLM_VSCODE_LM_ADAPTER: "in-process bridge builds no provider argv",
        DEEPSEEK_VSCODE_LM_ADAPTER: "in-process bridge builds no provider argv",
    }
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
        "grok_kilo_cli": "xai",
        OPENCODE_CLI_ADAPTER: "opencode",
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


def adapter_enforces_raw_discovery_denies(adapter_id: str) -> bool:
    """Return True when this adapter's launch argv actually denies raw search.

    True only for a transport whose CLI was verified to expose a tool-deny
    flag that ``build_runtime_command`` passes (see
    ``RAW_DISCOVERY_ENFORCING_ADAPTERS``).  An unknown or unlisted adapter
    returns False, so an unverified transport is never credited with
    enforcement it does not perform.  This is a pure capability statement and
    starts no process.
    """

    return adapter_id in RAW_DISCOVERY_ENFORCING_ADAPTERS


def raw_discovery_enforcement_fact(adapter_id: str) -> dict[str, Any]:
    """Return the launch-time raw-discovery enforcement fact for an adapter.

    Inert evidence for routing and launch records: ``enforced`` says whether
    the built argv carries the deny, and ``reason`` names the verified flag
    that carries it, or why the transport has none.  A transport that cannot
    deny is reported honestly rather than being handed a flag that would be
    silently ignored.
    """

    return {
        "adapter_id": adapter_id,
        "enforced": adapter_enforces_raw_discovery_denies(adapter_id),
        "reason": _RAW_DISCOVERY_ENFORCEMENT_REASONS.get(
            adapter_id, RAW_DISCOVERY_ENFORCEMENT_UNVERIFIED
        ),
        "denied_commands": list(RAW_DISCOVERY_DENIED_COMMANDS),
    }


# ---------------------------------------------------------------------------
# CLOSED TOOL SURFACE -- a SECOND, independent enforcement mechanism.
#
# ``adapter_enforces_raw_discovery_denies`` above is, and stays, a statement
# about launch ARGV: does the built command line carry a tool-deny flag.  It is
# read by routing and by launch records that need exactly that meaning, so it
# is not widened here.
#
# But argv is not the only way a rule can be enforced, and for one family it is
# not the relevant one.  The ``*_vscode_lm`` adapters build no provider argv at
# all: AIWorkHub is itself the tool server.  Verified 2026-09-08 against the
# shipped extension (``vscode-extension/extension.js``):
#
#   * ``options.tools`` for every request is ``vscodeLmToolsForRequest(...)``
#     (:5140), drawn only from the frozen ``VSCODE_LM_PRIVATE_TOOLS`` array
#     (:3195) -- 20 entries, every one an ``aiworkhub_*`` tool, plus the two
#     bridge-internal edit tools ``aiworkhub_manager_semantic_edit_stage`` and
#     ``..._finalize``.  The VS Code Language Model API offers a model only the
#     tools passed in ``options.tools``; the editor's own tool registry is
#     never read here, so nothing else is reachable.
#   * A returned tool call is re-checked against that offered set
#     (``permittedToolNames``, :5304) and against ``VSCODE_LM_PRIVATE_TOOLS``
#     (:3650), so an invented name is refused rather than executed.
#   * The Python side agrees: ``process_launcher.invoke_vscode_lm_worker_tool``
#     dispatches a fixed chain of ``tool_name == "<literal>"`` comparisons and
#     falls through to ``worker_bridge_tool_not_allowed``.
#
# There is therefore NO ``Grep``, ``Glob``, ``Edit`` or ``Write`` on that
# surface -- not denied, ABSENT -- which is a stronger guarantee than any deny
# flag, because a tool that is never offered cannot be called, mis-spelled
# around, or re-enabled by provider configuration.  Reporting this family as
# "unenforced" because it carries no argv flag inverts the truth.
#
# The one honest boundary: this describes the model AIWorkHub drives through
# ``sendRequest``.  It says nothing about what a human's own chat session may
# do in the same editor -- that is not this worker, and no adapter fact covers
# it.
# ---------------------------------------------------------------------------
CLOSED_TOOL_SURFACE_ADAPTERS: frozenset[str] = frozenset(
    {VSCODE_LM_ADAPTER, GLM_VSCODE_LM_ADAPTER, DEEPSEEK_VSCODE_LM_ADAPTER}
)
TOOL_SURFACE_MECHANISM_DISPATCH = "closed_dispatch_surface"
TOOL_SURFACE_MECHANISM_ARGV = "argv_tool_deny"
TOOL_SURFACE_MECHANISM_NONE = "none_at_launch"

# What each adapter's LAUNCH actually does about the raw file editor.  Stated
# per adapter rather than derived from "carries some deny flag", because the
# two are not the same question and conflating them overclaims.
#
#   absent_from_surface        the raw editor is not among the offered tools.
#   modify_denied_create_open  ``Edit`` is denied; ``Write`` is deliberately
#                              kept, so creating a new file and rewriting most
#                              of one -- the mandate's own exceptions -- remain
#                              possible, and Write-over-existing stays open and
#                              is recorded rather than blocked.
#   not_denied                 nothing at launch touches the editor.
RAW_EDITOR_ABSENT_FROM_SURFACE = "absent_from_surface"
RAW_EDITOR_MODIFY_DENIED_CREATE_OPEN = "modify_denied_create_open"
RAW_EDITOR_NOT_DENIED = "not_denied"
_RAW_EDITOR_DENY_STATES: Mapping[str, str] = MappingProxyType(
    {
        "claude_cli": RAW_EDITOR_MODIFY_DENIED_CREATE_OPEN,
        # Verified 2026-09-08 from ``copilot help permissions`` on the installed
        # CLI: the ``write`` permission kind "matches tools that create and
        # modify files".  One kind covers both, so denying modify-an-existing
        # would also deny new-file creation.  Left undenied deliberately.
        DEEPSEEK_COPILOT_ADAPTER: RAW_EDITOR_NOT_DENIED,
        GLM_COPILOT_ADAPTER: RAW_EDITOR_NOT_DENIED,
        "codex_cli": RAW_EDITOR_NOT_DENIED,
        GROK_KILO_ADAPTER: RAW_EDITOR_NOT_DENIED,
        OPENCODE_CLI_ADAPTER: RAW_EDITOR_NOT_DENIED,
        VSCODE_LM_ADAPTER: RAW_EDITOR_ABSENT_FROM_SURFACE,
        GLM_VSCODE_LM_ADAPTER: RAW_EDITOR_ABSENT_FROM_SURFACE,
        DEEPSEEK_VSCODE_LM_ADAPTER: RAW_EDITOR_ABSENT_FROM_SURFACE,
        # No launch of ours at all: a human drives this one. Named rather than
        # left to the default, so the ninth adapter is a stated fact.
        "deepseek_manual": RAW_EDITOR_NOT_DENIED,
    }
)


def adapter_serves_a_closed_tool_surface(adapter_id: str) -> bool:
    """Return True when AIWorkHub itself decides every tool this adapter offers.

    A pure capability statement, like its argv sibling, and unlisted defaults
    to False so a new transport can never inherit a guarantee nobody verified.
    """

    return adapter_id in CLOSED_TOOL_SURFACE_ADAPTERS


def tool_surface_enforcement_fact(adapter_id: str) -> dict[str, Any]:
    """How -- if at all -- this adapter's raw tool denies are enforced at launch.

    Three outcomes, not two.  ``closed_dispatch_surface`` means the raw tools
    are absent from the offered set; ``argv_tool_deny`` means the built command
    line carries a verified deny flag; ``none_at_launch`` means neither, and
    the only boundary left is what AIWorkHub records and finalizes afterwards.
    Inert evidence: it starts no process and reads no configuration.

    Search and EDITING are reported separately and must not be collapsed.  The
    Copilot pair carries a verified argv deny, but that deny names ``grep``
    and ``glob`` only: verified 2026-09-08 against the installed CLI, Copilot's
    permission kind ``write`` "matches tools that create and modify files", one
    kind for both, so there is no way to deny modifying an existing file
    without also denying new-file creation -- and creating a new file is one of
    the mandate's own exceptions.  Reporting those two adapters as
    editor-enforced because they carry SOME deny would be exactly the overclaim
    this module exists to prevent.
    """

    closed = adapter_serves_a_closed_tool_surface(adapter_id)
    argv_deny = adapter_enforces_raw_discovery_denies(adapter_id)
    if closed:
        mechanism = TOOL_SURFACE_MECHANISM_DISPATCH
        reason = (
            "AIWorkHub is the tool server: the offered set is a fixed "
            "aiworkhub_* allowlist, so raw search and the raw editor are "
            "absent rather than denied"
        )
    elif argv_deny:
        mechanism = TOOL_SURFACE_MECHANISM_ARGV
        reason = _RAW_DISCOVERY_ENFORCEMENT_REASONS.get(
            adapter_id, RAW_DISCOVERY_ENFORCEMENT_UNVERIFIED
        )
    else:
        mechanism = TOOL_SURFACE_MECHANISM_NONE
        reason = _RAW_DISCOVERY_ENFORCEMENT_REASONS.get(
            adapter_id, RAW_DISCOVERY_ENFORCEMENT_UNVERIFIED
        )
    return {
        "adapter_id": adapter_id,
        "mechanism": mechanism,
        "reason": reason,
        "raw_discovery_reachable": not (closed or argv_deny),
        "raw_editor_deny": _RAW_EDITOR_DENY_STATES.get(
            adapter_id, RAW_EDITOR_NOT_DENIED
        ),
        "argv_deny_enforced": argv_deny,
        "closed_tool_surface": closed,
    }


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
    if any(key not in ADAPTER_EXECUTABLES for key in executable_overrides):
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


def _default_kilo_extension_roots() -> tuple[Path, ...]:
    """Return only the bounded editor extension roots Kilo officially uses."""

    home = Path.home()
    return (
        home / ".vscode" / "extensions",
        home / ".vscode-server" / "extensions",
        home / ".vscode-server-insiders" / "extensions",
    )


def _kilo_extension_version(path: Path) -> tuple[int, ...]:
    match = re.search(r"kilocode\.kilo-code-(\d+(?:\.\d+)*)", path.name)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def _resolve_kilo_extension_executable(adapter_id: str) -> ExecutableResolution:
    binary_name = platform_io.executable_name("kilo")
    candidates: list[Path] = []
    for root in _default_kilo_extension_roots():
        if root.is_symlink() or not root.is_dir():
            continue
        try:
            extension_dirs = sorted(
                root.glob(_KILO_EXTENSION_DIR_GLOB), key=lambda path: path.name
            )[:128]
        except OSError:
            continue
        for extension_dir in extension_dirs:
            candidate = extension_dir / "bin" / binary_name
            if (
                extension_dir.is_symlink()
                or candidate.parent.is_symlink()
                or candidate.is_symlink()
            ):
                continue
            if candidate.is_file() and os.access(candidate, os.X_OK):
                candidates.append(candidate)
    if not candidates:
        return ExecutableResolution(
            adapter_id, None, False, "executable not found: kilo"
        )
    selected = max(
        candidates,
        key=lambda path: (_kilo_extension_version(path.parent.parent), str(path)),
    )
    try:
        resolved = selected.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return ExecutableResolution(
            adapter_id, None, False, "discovered Kilo executable is not a file"
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

    if adapter_id == OPENCODE_CLI_ADAPTER and _is_windows_host():
        return ExecutableResolution(
            adapter_id, None, False, OPENCODE_WINDOWS_RESOLUTION_FAIL_CLOSED
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
        if adapter_id == GROK_KILO_ADAPTER:
            return _resolve_kilo_extension_executable(adapter_id)
        if adapter_id == OPENCODE_CLI_ADAPTER and _is_linux_host():
            try:
                wrapper = Path(OPENCODE_SNAP_BIN)
                resolved = wrapper.resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                return ExecutableResolution(
                    adapter_id, None, False, f"executable not found: {binary}"
                )
            if not resolved.is_file() or not os.access(resolved, os.X_OK):
                return ExecutableResolution(
                    adapter_id,
                    None,
                    False,
                    f"discovered executable is not executable: {binary}",
                )
            return ExecutableResolution(adapter_id, str(wrapper), True, "")
        return ExecutableResolution(
            adapter_id, None, False, f"executable not found: {binary}"
        )

    try:
        discovered_path = Path(discovered)
        resolved = discovered_path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return ExecutableResolution(
            adapter_id, None, False, f"discovered executable is not a file: {binary}"
        )

    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return ExecutableResolution(
            adapter_id, None, False, f"discovered executable is not executable: {binary}"
        )

    executable = (
        str(discovered_path) if adapter_id == OPENCODE_CLI_ADAPTER else str(resolved)
    )
    return ExecutableResolution(adapter_id, executable, True, "")


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


def resolve_grok_kilo_model(model: str | None) -> tuple[str | None, str | None]:
    """Resolve the single xAI model supported by the Kilo CLI adapter."""

    if model is None:
        return GROK_KILO_DEFAULT_MODEL, None
    if not isinstance(model, str) or not model.strip() or "\x00" in model:
        return None, "unsupported_grok_kilo_model:malformed"
    candidate = model.strip()
    if candidate not in GROK_KILO_SUPPORTED_MODELS:
        return None, (
            "unsupported_grok_kilo_model:"
            f"{candidate}:allowed={'|'.join(GROK_KILO_SUPPORTED_MODELS)}"
        )
    return candidate, None


def resolve_opencode_model(model: str | None) -> tuple[str | None, str | None]:
    """Preserve an exact OpenCode ``provider/model`` identity; no inventory."""

    if not isinstance(model, str) or not model.strip() or "\x00" in model:
        return None, "unsupported_opencode_model:malformed"
    candidate = model.strip()
    if any(ch.isspace() for ch in candidate):
        return None, "unsupported_opencode_model:malformed"
    provider, sep, remainder = candidate.partition("/")
    if not sep or not provider or not remainder or remainder.startswith("/"):
        return None, "unsupported_opencode_model:missing_provider_or_model"
    return candidate, None


def _is_glm_family(name: str) -> bool:
    """True when ``name`` normalizes into the GLM provider family."""

    return _normalize_editor_token(name).startswith("glm")


def resolve_glm_model(
    model: str | None,
    observed_models: Iterable[str] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve and guard a GLM model selection against the editor-discovered set.

    ``observed_models`` is the set of models the editor actually reported (a
    discovery result). When it is provided, ONLY a reported model resolves; any
    other name is refused BY NAME together with the reported catalog -- never
    silently accepted and never silently dropped. When it is ``None`` -- a
    cold start, or a route with no editor catalog such as Copilot BYOK -- any
    well-formed GLM-family id is accepted so the route's own authoritative gate
    (bridge readiness or the credential layer) can confirm it, and a missing
    selection defaults to the single named cold-start fallback. No enumerated
    list of GLM names gates this: the vocabulary is family shape + discovery.
    """
    if model is None:
        candidate = GLM_COLD_START_FALLBACK_MODEL
    else:
        if not isinstance(model, str) or not model.strip():
            return None, "model must be a nonempty string when provided"
        if "\x00" in model:
            return None, "model contains a NUL character"
        candidate = model.strip()
    if observed_models is not None:
        observed = [str(value).strip() for value in observed_models if str(value).strip()]
        if candidate not in observed:
            catalog = "|".join(sorted(set(observed))) or "none"
            return None, (
                "unsupported_glm_model:"
                f"{candidate}:not_reported_by_editor:observed={catalog}"
            )
        return candidate, None
    if not editor_requested_name_ok(candidate) or not _is_glm_family(candidate):
        return None, f"unsupported_glm_model:{candidate}:not_glm_family_or_malformed"
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
    # Every platform branch belongs to platform_io.  This stays a named
    # function only because it is the seam the adapter tests inject a host
    # platform through; the decision itself is made in exactly one module.
    return platform_io.is_windows()


def _is_linux_host() -> bool:
    return platform_io.is_linux()


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


_WORKER = "mcp__aiworkhub_worker_ai_tools__aiworkhub_worker_"

# Tools any worker may hold, whatever its role.
CLAUDE_READ_TOOLS: tuple[str, ...] = (
    "Read",
    "Bash",
    f"{_WORKER}source_graph_query",
    f"{_WORKER}session_current_state",
    f"{_WORKER}ai_memory_search",
    f"{_WORKER}ai_memory_get",
    f"{_WORKER}ai_memory_related",
    f"{_WORKER}kb_search",
    f"{_WORKER}kb_get",
    f"{_WORKER}kb_related",
    f"{_WORKER}validation_output_page",
    f"{_WORKER}exit_preflight",
)

# Tools that change something. A read-only reviewer must not be handed one:
# its card says read_only with an empty allowed_writes and repository_write
# forbidden, and the sandbox enforces that -- so offering Write, Edit and
# semantic_edit_apply could only ever produce a denial. Measured on reviewer
# request 5415654189de: seven write tools offered, zero used, and the
# candidate-review turn budget spent partly on being refused.
#
# ``Edit`` is deliberately ABSENT here and denied at launch (see
# ``CLAUDE_WORKER_RAW_EDITOR_DENIES``).  Modifying an existing file is exactly
# what ``semantic_edit_prepare``/``_apply`` replaces, so granting the raw
# spelling beside them left the only repository rule whose alternative was
# still on the tool list -- and it is the only one that did not take (pooled
# path coverage 9.1% over 2,648 gate-verified attempts).  ``Write`` STAYS:
# two of the three policy exceptions -- a new file, and a change spanning most
# of a file -- have no semantic-edit form at all, so denying it would delete
# the exception rather than the shortcut.
CLAUDE_WRITE_TOOLS: tuple[str, ...] = (
    "Write",
    f"{_WORKER}validation_run",
    f"{_WORKER}semantic_edit_prepare",
    f"{_WORKER}semantic_edit_apply",
    f"{_WORKER}semantic_edit_exception_declare",
    f"{_WORKER}session_write_intent",
    f"{_WORKER}ai_memory_write_intent",
    f"{_WORKER}kb_write_intent",
)

# The reviewer's own submission channel. A build worker has nothing to submit
# through it, and holding it invites a worker to file a review of itself.
CLAUDE_REVIEW_TOOLS: tuple[str, ...] = (
    f"{_WORKER}quality_review_packet_read",
    f"{_WORKER}quality_review_submit",
)

# Native search for the read-only reviewer only.  The same reasoning that
# stripped the write tools above applies to a deny the sandbox makes
# pointless: the reviewer sandbox is Landlock read-only with an empty
# allowed_writes, so ``Grep`` and ``Glob`` cannot change anything, and denying
# them protected nothing.  Measured over 242 claude_cli reviewer runs
# (2026-09-08 reviewer audit): 1,080 Bash permission denials -- 25% of every
# reviewer Bash call, in 181/242 runs (804 grep, 96 find, 52 rg) -- each a
# wasted turn with zero information, followed by python3 -c file readers,
# Agent subagents (110 in 60 runs), sandbox probes and whole-file Reads as
# substitutes, ~285K input tokens per run.  Build workers keep the full deny:
# Source Graph is their discovery path and the repository policy holds them to
# it.  The raw shell forms stay denied for reviewers too -- the bounded native
# tools are the substitute, not an unbounded shell scan.
CLAUDE_REVIEWER_SEARCH_TOOLS: tuple[str, ...] = CLAUDE_RAW_DISCOVERY_TOOL_DENIES

# Host tools a reviewer must not hold.  ``ReportFindings`` is the host's own
# code-review channel: 73/242 claude reviewer runs (30%) filed findings
# through it (90 calls, 13 InputValidationErrors), 66 typed the same findings
# twice, and 9 runs filed ONLY there -- where ingest never looked -- losing
# the whole 1.5M-token review.  ``ScheduleWakeup`` (15 calls) schedules a
# turn no supervisor will ever answer.  The one authoritative channel is the
# final JSON report the supervisor ingests and submits.
CLAUDE_REVIEWER_HOST_TOOL_DENIES: tuple[str, ...] = ("ReportFindings", "ScheduleWakeup")

# Validation commands a BUILD worker must route through
# ``aiworkhub_worker_validation_run`` rather than raw Bash.  Measured over 659
# parseable worker runs on 2026-09-08: validation was 42.7% of every
# tool-result byte on 16.1% of calls; 170 single Codex calls carried more than
# 100 KB each (74.9 MB) and one pytest call returned 988,306 bytes; 550
# IDENTICAL commands were re-run inside one run (14.7 MB) across 210 of 478
# runs; and only 39-49% of the commands typed were the card's declared command.
# The MCP tool resolves the declared command itself, memoises on the current
# candidate bytes and returns a bounded record, so the raw spellings are denied
# the same way raw discovery is.
#
# Scope, stated exactly: these are Claude Bash PREFIX rules, so they deny the
# bare console-script spellings (``pytest ...``, ``ruff ...``, ``mypy ...``)
# that the audit found among 109 distinct first tokens.  They cannot deny a
# ``<python> -m pytest`` spelling -- no prefix rule can -- which is why the
# worker runtime policy states the rule in words as well.  A read-only reviewer
# is unaffected: it holds no card validation to route.
WORKER_VALIDATION_DENIED_COMMANDS: tuple[str, ...] = ("pytest", "ruff", "mypy")
CLAUDE_WORKER_VALIDATION_SHELL_DENIES: tuple[str, ...] = tuple(
    f"Bash({command} *)" for command in WORKER_VALIDATION_DENIED_COMMANDS
)

# The raw file EDITOR a build worker must not hold.  Measured 2026-09-08 over
# 2,648 gate-verified attempts that changed at least one file: 1,500 made ZERO
# semantic-edit applies and pooled path coverage was 9.1% (codex_cli 13.1%,
# claude_cli 12.6%, deepseek_copilot_cli 0.6%) -- while every one of those
# adapters was granted ``semantic_edit_prepare`` and ``_apply``, so missing
# tools explain none of it.  That whole window PREDATES the mandate (the policy
# section landed 2026-09-08 19:14Z; the last measured attempt ran 14:52Z, under
# a prompt that said "prefer"), so 9.1% is a baseline, not disobedience.  What
# separates this rule from the two that DID take is the shape of the fix:
# search compliance was won by DELETING Grep/Glob and validation compliance by
# denying the raw pytest/ruff/mypy spellings, whereas editing was asked for in
# prose with ``Edit`` left on the tool list beside the semantic pair.
#
# Scope, stated exactly, because a deny that overclaims is worse than none.
# The per-tool numbers below are from 215 retained claude_cli worker runs
# (2026-09-02 .. 2026-09-08); they are a floor for that slice, not a census.
#
# * ``Edit`` -- 547 calls, and 523 of them (95.6%) hit a file for which
#   ``semantic_edit_prepare`` was NEVER called anywhere in that run.  Edit is
#   not a fallback after a failed prepare, it is the FIRST choice, so this is
#   the leak.  It also only ever modifies a file that already exists, which is
#   exactly what prepare/apply performs with a hash precondition -- a total
#   substitute.  Denying it removes the shortcut precisely where the
#   replacement is complete.
# * ``Write`` is deliberately NOT denied -- 79 calls over 60 distinct targets,
#   of which 49 (82%) were authoring a NEW file and only 11 overwrote existing
#   content.  Of those 11, eight were rework attempts re-authoring a file the
#   card itself creates; just two were true whole-file rewrites of a canonical
#   pre-existing file.  Write is the new-file channel, and new files are one of
#   the mandate's own exceptions, so denying it would delete the exception
#   rather than the shortcut.
# * Write-over-an-existing-file therefore stays OPEN, measured at 2 of 60
#   targets.  It is not closed, it is RECORDED:
#   ``process_launcher._semantic_edit_coverage`` reports a changed path with no
#   apply receipt and no declaration as ``undeclared_raw_only``, and
#   ``aiworkhub_worker_semantic_edit_exception_declare`` is how a worker moves
#   such a path from undeclared to declared.  Nothing here gates on either.
# * ``Bash`` is NOT a third hole today: across 2,445 Bash calls in that slice
#   there were zero ``sed -i``, zero redirections into the worktree and zero
#   ``mv``/``cp`` into it.  So the earlier hypothesis that denying ``Write``
#   would simply relocate whole-file writes into the shell is NOT supported by
#   the data, and is not the reason for keeping it -- the reason is the new-file
#   exception above.  What the data does show is that ``Write`` sits one step
#   from becoming the substitute once ``Edit`` is gone, which is why the
#   residual is re-measured after this change rather than declared closed.
#   Constraining ``Write`` to absent-or-empty targets cannot be expressed in a
#   tool-name deny list and needs its own mechanism; it is deliberately not
#   attempted here.
#
# The extra spellings cost nothing: an unknown tool NAME in a deny list is
# inert, unlike an unknown CLI FLAG, which is accepted and silently ignored.
# ``MultiEdit`` was never observed in the retained slice (0 calls); it and
# ``NotebookEdit`` are listed because they are the same operation under other
# names, not because they were seen.
CLAUDE_WORKER_RAW_EDITOR_DENIES: tuple[str, ...] = (
    "Edit",
    "MultiEdit",
    "NotebookEdit",
)


def claude_allowed_tools(*, read_only: bool) -> tuple[str, ...]:
    """Tools this role can actually use -- never the union of every role.

    Every worker used to receive one flat list: a strictly read-only reviewer
    got Write, Edit and semantic_edit_apply, and a build worker got the
    reviewer's submit channel. Each tool's schema is prompt text the model
    pays for on every turn, and a tool the sandbox will refuse is worse than
    absent -- it is an invitation to spend a turn discovering that.
    """

    if read_only:
        return (*CLAUDE_READ_TOOLS, *CLAUDE_REVIEWER_SEARCH_TOOLS, *CLAUDE_REVIEW_TOOLS)
    return (*CLAUDE_READ_TOOLS, *CLAUDE_WRITE_TOOLS)


def claude_disallowed_tools(*, read_only: bool) -> tuple[str, ...]:
    """The ``--disallowedTools`` list for this role.

    A build worker is denied raw discovery in every form (native ``Grep``/
    ``Glob`` and the shell commands), the raw validation spellings it must
    route through ``aiworkhub_worker_validation_run``, and the raw file EDITOR
    it must route through ``semantic_edit_prepare``/``_apply`` (see
    ``CLAUDE_WORKER_RAW_EDITOR_DENIES``, which records exactly what that deny
    does and does not close).  A read-only reviewer keeps the discovery shell
    denies, is granted the native search tools instead (see
    ``CLAUDE_REVIEWER_SEARCH_TOOLS``), and is additionally denied the host
    tools that would file its report where the supervisor never reads; it
    holds no card validation to route and no tree it may write, so neither the
    validation nor the editor denies apply to it.
    """

    if read_only:
        return (*CLAUDE_RAW_DISCOVERY_SHELL_DENIES, *CLAUDE_REVIEWER_HOST_TOOL_DENIES)
    return (
        *CLAUDE_RAW_DISCOVERY_DENIES,
        *CLAUDE_WORKER_VALIDATION_SHELL_DENIES,
        *CLAUDE_WORKER_RAW_EDITOR_DENIES,
    )


OPENCODE_WORKER_MCP_TOOLS: tuple[str, ...] = (
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_session_current_state",
    "aiworkhub_worker_ai_memory_search",
    "aiworkhub_worker_ai_memory_get",
    "aiworkhub_worker_ai_memory_related",
    "aiworkhub_worker_kb_search",
    "aiworkhub_worker_kb_get",
    "aiworkhub_worker_kb_related",
    "aiworkhub_worker_validation_output_page",
    "aiworkhub_worker_exit_preflight",
    "aiworkhub_worker_validation_run",
    "aiworkhub_worker_semantic_edit_prepare",
    "aiworkhub_worker_semantic_edit_apply",
    "aiworkhub_worker_semantic_edit_exception_declare",
    "aiworkhub_worker_session_write_intent",
    "aiworkhub_worker_ai_memory_write_intent",
    "aiworkhub_worker_kb_write_intent",
)
OPENCODE_DENIED_BUILTIN_TOOLS: tuple[str, ...] = (
    "read",
    "edit",
    "bash",
    "task",
    "skill",
    "lsp",
    "websearch",
    "glob",
    "grep",
    "webfetch",
    "question",
    "external_directory",
    "doom_loop",
)


def opencode_mcp_tool_name(mcp_tool: str) -> str:
    return f"{OPENCODE_WORKER_MCP_SERVER}_{mcp_tool}"


def opencode_worker_permission_contract() -> dict[str, str]:
    permission: dict[str, str] = {"*": OPENCODE_PERMISSION_DENY}
    for tool in OPENCODE_DENIED_BUILTIN_TOOLS:
        permission[tool] = OPENCODE_PERMISSION_DENY
    for mcp_tool in OPENCODE_WORKER_MCP_TOOLS:
        permission[opencode_mcp_tool_name(mcp_tool)] = OPENCODE_PERMISSION_ALLOW
    return permission


def _opencode_permission_pattern_matches(pattern: str, name: str) -> bool:
    escaped = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.fullmatch(escaped, name) is not None


def opencode_permission_action(tool_name: str) -> str:
    action = OPENCODE_PERMISSION_DENY
    for pattern, value in opencode_worker_permission_contract().items():
        if _opencode_permission_pattern_matches(pattern, tool_name):
            action = value
    return action


def opencode_tool_is_allowed(tool_name: str) -> bool:
    return opencode_permission_action(tool_name) == OPENCODE_PERMISSION_ALLOW


def build_opencode_worker_mcp_config(mcp_command: Sequence[str]) -> dict[str, Any]:
    if isinstance(mcp_command, (str, bytes)) or not isinstance(mcp_command, Sequence):
        raise ValueError("opencode_mcp_command_must_be_argv")
    command: list[str] = []
    for token in mcp_command:
        if not isinstance(token, str) or not token or "\x00" in token:
            raise ValueError("opencode_mcp_command_token_invalid")
        command.append(token)
    if not command:
        raise ValueError("opencode_mcp_command_empty")
    return {
        "$schema": "https://opencode.ai/config.json",
        "permission": opencode_worker_permission_contract(),
        "mcp": {
            OPENCODE_WORKER_MCP_SERVER: {
                "type": "local",
                "command": command,
                "enabled": True,
            }
        },
    }


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
    read_only: bool = False,
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

    if model is not None and adapter_id != GROK_KILO_ADAPTER:
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
    if (
        _is_windows_host()
        and outer_sandbox_backend != WINDOWS_APPCONTAINER_SANDBOX_BACKEND
    ):
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
            *claude_allowed_tools(read_only=read_only),
            "--no-session-persistence",
            "--disallowedTools",
            *claude_disallowed_tools(read_only=read_only),
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
    elif adapter_id == GROK_KILO_ADAPTER:
        resolved_model, model_error = resolve_grok_kilo_model(model)
        if model_error:
            return _invalid_plan(adapter_id, model_error, cwd=cwd)
        assert resolved_model is not None
        argv = [
            executable,
            "run",
            "--pure",
            "--model",
            resolved_model,
            "--format",
            "json",
            "--dir",
            cwd,
            "--auto",
            prompt,
        ]
    elif adapter_id == OPENCODE_CLI_ADAPTER:
        resolved_model, model_error = resolve_opencode_model(model)
        if model_error:
            return _invalid_plan(adapter_id, model_error, cwd=cwd)
        assert resolved_model is not None
        argv = [
            executable,
            "run",
            "--format",
            "json",
            "--model",
            resolved_model,
            prompt,
        ]
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
    read_only: bool = False,
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
        read_only=read_only,
    )


# ---------------------------------------------------------------------------
# Provider refusal vs worker crash classification.
#
# A launched worker that exits non-zero was, until now, always recorded as
# ``worker_failed`` exit_code 1 -- so a provider that REFUSED the turn (a 429
# session limit, a dead balance, an expired credential) was indistinguishable
# from a genuine crash in the worker's own code.  The operator was then told
# "the worker failed" when the truth was "the provider would not serve the
# request", and an exhausted-quota refusal -- which becomes serviceable again
# the moment its reported reset window elapses -- was treated as a permanent
# worker bug.
#
# ``classify_provider_outcome`` separates the two from the provider's own exit
# code and message.  A refusal carries the provider's verbatim message, names
# its shape, and -- for an exhausted quota/session/rate limit -- is marked
# operationally recoverable and carries the reset window it reported so a retry
# respects that window instead of hammering a limited endpoint.  A refusal
# whose window is not reported is still recoverable in principle but is flagged
# ``reset_reported`` False so a caller never retries immediately without a bound.
# ---------------------------------------------------------------------------
PROVIDER_OUTCOME_SCHEMA_ID = "aiworkhub.provider_outcome.v1"

OUTCOME_OK = "ok"
OUTCOME_PROVIDER_REFUSED = "provider_refused"
OUTCOME_WORKER_FAILED = "worker_failed"

# Refusal shapes.  ``session_limit``, ``quota_exhausted`` and ``rate_limited``
# recover once their reported window resets; ``balance_exhausted`` is a dead
# account -- an HTTP 402 or an ``insufficient_balance`` body -- and needs credit,
# not a wait, so time alone never recovers it and it is emphatically NOT rate
# limiting or usage-quota exhaustion (NF-2026-00275 rework); ``credential_rejected``
# is a refusal too but needs a new credential; ``provider_unavailable`` is a
# transient upstream outage.
REFUSAL_SESSION_LIMIT = "session_limit"
REFUSAL_QUOTA_EXHAUSTED = "quota_exhausted"
REFUSAL_BALANCE_EXHAUSTED = "balance_exhausted"
REFUSAL_RATE_LIMITED = "rate_limited"
REFUSAL_CREDENTIAL_REJECTED = "credential_rejected"
REFUSAL_PROVIDER_UNAVAILABLE = "provider_unavailable"
# A bare 401/403 whose body names no cause: authentication, quota and rate
# limiting are indistinguishable from the status code alone, so the classifier
# records that the cause was not distinguished rather than guessing one of them.
# This is the honest verdict for the NF-2026-00326 case -- a credential that
# launched nine workers, then hit an HTTP 401 with an empty body, then cleared
# on its own is a quota/rate condition, not a bad key, and the status code can
# never prove which.
REFUSAL_CAUSE_NOT_DISTINGUISHED = "cause_not_distinguished"

_RECOVERABLE_REFUSALS: frozenset[str] = frozenset(
    {REFUSAL_SESSION_LIMIT, REFUSAL_QUOTA_EXHAUSTED, REFUSAL_RATE_LIMITED}
)

# HTTP 402 PAYMENT REQUIRED is never a recoverable wait, whatever the body
# says: only added credit clears it, never elapsed time.  This is enforced as a
# status-level invariant so the "wait, it will clear" verdict can never attach
# to a dead account even if a future body token would otherwise pull a 402 into
# a recoverable kind -- token membership has been wrong about this twice, so the
# guard no longer depends on it (NF-2026-00275 rework round four).
_NEVER_RECOVERABLE_STATUSES: frozenset[int] = frozenset({402})

# Body tokens that name a concrete cause.  These are matched against the
# provider's OWN response body -- which, at the launch boundary, is the
# provider's machine error code (e.g. ``insufficient_balance``) rather than
# prose.  Tokens are therefore written in a single canonical space form and the
# text under test is normalised (``_`` and ``-`` -> space) before matching, so a
# machine code and a prose message that mean the same thing match the same
# token.  This is what keeps the launch-time detector and this classifier on ONE
# vocabulary: the detector reuses ``provider_body_names_cause`` below rather than
# maintaining a parallel token list that could drift (NF-2026-00275 rework).  The
# HTTP status code alone is deliberately NOT treated as a credential signal,
# because a dead key, an expired token and a transient rate/quota condition can
# all surface as an identical bare 401.
#
# ``balance`` is split out from ``quota``: a dead account (HTTP 402, an
# ``insufficient_balance`` code, ``out of credit``) is recovered by adding
# credit, never by waiting, so it must NOT be lumped with usage-quota exhaustion
# or reported as a recoverable rate limit.  Balance is checked before quota and
# rate so that a body naming a balance condition classifies as balance even when
# the status code says 429.
_BALANCE_TOKENS: tuple[str, ...] = (
    "insufficient balance",
    "insufficient funds",
    "insufficient credit",
    "insufficient credits",
    "out of credit",
    "out of credits",
    "balance exhausted",
    "credit exhausted",
    "credits exhausted",
    "billing hard limit",
    "payment required",
)
# ``exhausted`` is deliberately NOT a bare quota token: quota, balance, credits
# and sessions can all be "exhausted", so on its own it names nothing and would
# capture whichever branch reached it first -- which is how a 402/429 body
# saying ``balance_exhausted`` was once mis-reported as a recoverable quota wait
# (NF-2026-00275 rework round four).  The balance phrasings above carry the
# ``exhausted`` forms that DO name a dead account; quota keeps only the tokens
# that name usage-quota specifically.
_QUOTA_TOKENS: tuple[str, ...] = (
    "insufficient quota",
    "quota exceeded",
    "quota exhausted",
)
_RATE_TOKENS: tuple[str, ...] = (
    "rate limit",
    "too many requests",
)
# Concrete credential defects only -- a named bad/expired/revoked key or token.
# The bare HTTP reason phrase ("unauthorized") and generic "authentication"
# boilerplate are intentionally excluded: they accompany every 401 regardless of
# the real cause and so distinguish nothing.
_CREDENTIAL_TOKENS: tuple[str, ...] = (
    "invalid api key",
    "invalid x-api-key",
    "api key not valid",
    "api key is invalid",
    "no api key",
    "missing api key",
    "api key expired",
    "expired token",
    "expired credential",
    "credential expired",
    "token expired",
    "revoked",
)
_UNAVAILABLE_TOKENS: tuple[str, ...] = (
    "overloaded",
    "service unavailable",
    "server error",
)

# The launch-refusal cause families the detector forwards to the classifier.
# Provider-unavailable (5xx / "overloaded") is deliberately excluded: a transient
# upstream outage is left to the worker path, not treated as a launch refusal.
_LAUNCH_REFUSAL_BODY_TOKENS: tuple[str, ...] = (
    _BALANCE_TOKENS + _QUOTA_TOKENS + _RATE_TOKENS + _CREDENTIAL_TOKENS
    + ("session limit",)
)

# Provider-owned HTTP refusal statuses and the refusal kind each names.  The
# launch-time detector forwards exactly these statuses, and every one of them
# resolves to a kind here, so a status can never be forwarded and then land on
# ``worker_failed`` for lack of a branch (the NF-2026-00275 rework defect: 402
# was forwarded but unnamed).  402 is PAYMENT REQUIRED -> balance, not quota and
# not rate limiting.
_STATUS_REFUSAL_KIND: dict[int, str] = {
    401: REFUSAL_CAUSE_NOT_DISTINGUISHED,
    402: REFUSAL_BALANCE_EXHAUSTED,
    403: REFUSAL_CAUSE_NOT_DISTINGUISHED,
    429: REFUSAL_RATE_LIMITED,
}
PROVIDER_REFUSAL_STATUSES: frozenset[int] = frozenset(_STATUS_REFUSAL_KIND)


def _normalize_cause_text(text: str) -> str:
    """Lower-case and fold ``_``/``-`` to spaces so machine codes match tokens."""

    return text.lower().replace("_", " ").replace("-", " ")


def provider_body_names_cause(text: str) -> bool:
    """True when the provider's own body/machine code names a launch-refusal cause.

    Shared with ``process_launcher``'s launch-time detector so the gate that
    decides whether to forward a body to :func:`classify_provider_outcome` and
    the classifier that names it draw on ONE vocabulary and cannot drift onto
    different token forms again (NF-2026-00275 rework).
    """

    normalized = _normalize_cause_text(text)
    return any(token in normalized for token in _LAUNCH_REFUSAL_BODY_TOKENS)

_RETRY_AFTER_RE = re.compile(
    r"retry[\s_-]*after[\s:=]*([0-9]+)\s*(?:s|sec|secs|seconds)?", re.IGNORECASE
)
_RESET_IN_RE = re.compile(
    r"(?:try again in|resets? in|reset in)\s+([0-9]+)\s*(?:s|sec|secs|seconds)?",
    re.IGNORECASE,
)
_RESET_AT_RE = re.compile(
    r"resets?(?:\s+at)?\s+(\d{4}-\d{2}-\d{2}T[0-9:.+Z-]+)", re.IGNORECASE
)


def _provider_status_code(text: str) -> int | None:
    """Return the first standalone HTTP 4xx/5xx status token in ``text``."""

    for match in re.finditer(r"\b([0-9]{3})\b", text):
        code = int(match.group(1))
        if 400 <= code <= 599:
            return code
    return None


def _reset_window(text: str) -> tuple[int | None, str | None]:
    """Extract a reported reset window: ``(retry_after_seconds, reset_at)``."""

    retry_after: int | None = None
    reset_at: str | None = None
    after = _RETRY_AFTER_RE.search(text)
    if after:
        retry_after = int(after.group(1))
    if retry_after is None:
        reset_in = _RESET_IN_RE.search(text)
        if reset_in:
            retry_after = int(reset_in.group(1))
    reset = _RESET_AT_RE.search(text)
    if reset:
        reset_at = reset.group(1)
    return retry_after, reset_at


def _refusal_kind(lowered: str, status_code: int | None) -> str | None:
    """Name the refusal shape from the provider's body, or ``None`` if none.

    Body-derived signals are authoritative: the provider's own words name the
    cause, so they are checked first and BEAT the status code -- an
    ``insufficient_balance`` body is a dead account even when the status is 429,
    because the body is more specific than the status.  Balance is checked before
    quota and rate for the same reason a dead account must never be reported as a
    recoverable wait.  The HTTP status code is only a fallback, and 401/403 alone
    is deliberately NOT allowed to name ``credential_rejected`` -- a dead key, an
    expired token and a transient rate/quota condition are indistinguishable from
    a bare 401, so such a refusal is reported as ``cause_not_distinguished``
    rather than guessed (NF-2026-00326).  Token matching runs on ``_``/``-``
    normalised text so a machine error code matches the same token as its prose
    form (NF-2026-00275 rework).
    """

    normalized = _normalize_cause_text(lowered)
    if "session limit" in normalized:
        return REFUSAL_SESSION_LIMIT
    if any(token in normalized for token in _BALANCE_TOKENS):
        return REFUSAL_BALANCE_EXHAUSTED
    if any(token in normalized for token in _QUOTA_TOKENS):
        return REFUSAL_QUOTA_EXHAUSTED
    if any(token in normalized for token in _RATE_TOKENS):
        return REFUSAL_RATE_LIMITED
    if any(token in normalized for token in _CREDENTIAL_TOKENS):
        return REFUSAL_CREDENTIAL_REJECTED
    if any(token in normalized for token in _UNAVAILABLE_TOKENS):
        return REFUSAL_PROVIDER_UNAVAILABLE
    # Status-code fallbacks, used only where the body named no cause.  Every
    # provider-owned refusal status resolves through the shared status map, so a
    # status the detector forwards can never fall through to ``worker_failed``.
    if status_code in (500, 502, 503, 529):
        return REFUSAL_PROVIDER_UNAVAILABLE
    if status_code in _STATUS_REFUSAL_KIND:
        return _STATUS_REFUSAL_KIND[status_code]
    return None


def classify_provider_outcome(
    *, exit_code: int, message: str = "", stderr: str = ""
) -> dict[str, Any]:
    """Classify a worker exit as provider refusal, worker crash, or clean exit.

    ``message``/``stderr`` are the provider's own text; both are carried
    verbatim (bounded) as ``provider_message`` so an operator sees the real
    reason.  A refusal is distinct from a crash: a dead balance, an expired
    credential and a genuine ``ZeroDivisionError`` no longer collapse into one
    ``worker_failed`` verdict.  An exhausted-quota / session-limit / rate-limit
    refusal is ``recoverable`` and carries the reset window it reported
    (``retry_after_seconds`` / ``reset_at``); ``reset_reported`` is False when
    the provider named no window, so a caller must not retry without a bound.
    """

    parts = [part for part in (str(message or ""), str(stderr or "")) if part]
    text = "\n".join(parts)
    lowered = text.lower()
    provider_message = text[:1000]
    try:
        code = int(exit_code)
    except (TypeError, ValueError):
        code = 1

    if code == 0:
        return {
            "schema_id": PROVIDER_OUTCOME_SCHEMA_ID,
            "outcome": OUTCOME_OK,
            "refusal": False,
            "refusal_kind": "",
            "recoverable": False,
            "retry_after_seconds": None,
            "reset_at": None,
            "reset_reported": False,
            "provider_message": provider_message,
            "exit_code": code,
            "reason": "worker_exited_zero",
        }

    status_code = _provider_status_code(lowered)
    kind = _refusal_kind(lowered, status_code)
    if kind is None:
        return {
            "schema_id": PROVIDER_OUTCOME_SCHEMA_ID,
            "outcome": OUTCOME_WORKER_FAILED,
            "refusal": False,
            "refusal_kind": "",
            "recoverable": False,
            "retry_after_seconds": None,
            "reset_at": None,
            "reset_reported": False,
            "provider_message": provider_message,
            "exit_code": code,
            "status_code": status_code,
            "reason": "worker_process_failure_no_provider_refusal_signal",
        }

    recoverable = (
        kind in _RECOVERABLE_REFUSALS
        and status_code not in _NEVER_RECOVERABLE_STATUSES
    )
    retry_after, reset_at = _reset_window(text) if recoverable else (None, None)
    reset_reported = recoverable and (retry_after is not None or reset_at is not None)
    if kind == REFUSAL_CAUSE_NOT_DISTINGUISHED:
        # A bare 401/403: name the status and say explicitly that the response
        # did not distinguish authentication from quota or rate limiting, rather
        # than picking one of them as if it were established (NF-2026-00326).
        reason = (
            f"provider_refused:http_status={status_code}"
            ":cause_not_distinguished_by_response"
        )
    elif kind == REFUSAL_CREDENTIAL_REJECTED:
        reason = "provider_refused_credential_rejected_needs_new_credential"
    elif recoverable and reset_reported:
        reason = f"provider_refused_{kind}_recoverable_after_reported_window"
    elif recoverable:
        reason = f"provider_refused_{kind}_recoverable_but_reset_window_unreported"
    else:
        reason = f"provider_refused_{kind}"
    return {
        "schema_id": PROVIDER_OUTCOME_SCHEMA_ID,
        "outcome": OUTCOME_PROVIDER_REFUSED,
        "refusal": True,
        "refusal_kind": kind,
        "recoverable": bool(recoverable),
        "retry_after_seconds": retry_after,
        "reset_at": reset_at,
        "reset_reported": bool(reset_reported),
        "provider_message": provider_message,
        "exit_code": code,
        "status_code": status_code,
        "reason": reason,
    }


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
    "GLM_COLD_START_FALLBACK_MODEL",
    "GLM_SECRET_ENV_VAR",
    "GLM_SUPPORTED_MODELS",
    "GROK_KILO_ADAPTER",
    "GROK_KILO_DEFAULT_MODEL",
    "GROK_KILO_SUPPORTED_MODELS",
    "ROUTE_FAMILY_CLAUDE_CLI",
    "ROUTE_FAMILY_CODEX_CLI",
    "ROUTE_FAMILY_COPILOT_BYOK_CLI",
    "ROUTE_FAMILY_EDITOR_VSCODE_LM",
    "ROUTE_FAMILY_KILO_XAI_CLI",
    "ROUTE_FAMILY_UNKNOWN",
    "route_family",
    "EDITOR_NONCALLABLE_VENDORS",
    "EDITOR_NONCALLABLE_ID_PREFIXES",
    "EDITOR_REQUESTED_MODEL_RE",
    "editor_requested_name_ok",
    "editor_model_is_callable",
    "discover_callable_model_names",
    "LOCAL_ADAPTERS",
    "MANUAL_ONLY_ADAPTERS",
    "SUPPORTED_ADAPTERS",
    "VSCODE_LM_ADAPTER",
    "ExecutableResolution",
    "RuntimeAdapterPlan",
    "PROVIDER_OUTCOME_SCHEMA_ID",
    "OUTCOME_OK",
    "OUTCOME_PROVIDER_REFUSED",
    "OUTCOME_WORKER_FAILED",
    "REFUSAL_SESSION_LIMIT",
    "REFUSAL_QUOTA_EXHAUSTED",
    "REFUSAL_BALANCE_EXHAUSTED",
    "REFUSAL_RATE_LIMITED",
    "REFUSAL_CREDENTIAL_REJECTED",
    "REFUSAL_PROVIDER_UNAVAILABLE",
    "REFUSAL_CAUSE_NOT_DISTINGUISHED",
    "PROVIDER_REFUSAL_STATUSES",
    "build_adapter_command",
    "build_runtime_command",
    "classify_provider_outcome",
    "provider_body_names_cause",
    "inject_worker_mcp_config",
    "resolve_deepseek_model",
    "resolve_executable",
    "resolve_glm_model",
    "resolve_grok_kilo_model",
]
