"""Compact, deterministic AI tool-use instruction projections.

This module is pure: it renders and plans managed-block edits but never reads
or writes repository files.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any, Literal

# The enforced raw-discovery vocabulary and the per-adapter enforcement
# capability live in ``runtime_adapters``; the rendered prohibition is derived
# from them rather than restated, so a command added to the tuple reaches every
# projection and every worker prompt in the same change.
#
# Direction check before wiring it: ``runtime_adapters`` imports only
# ``platform_io`` (stdlib-only), and nothing in that chain imports this module,
# so this edge is acyclic.  The constraint that module documents -- "repo_policy
# already imports this one, so the dependency may only ever run in that
# direction" -- is about ``repo_policy``; it does not forbid this direction, and
# ``provider_tool_guards`` already imports THIS module while importing
# ``runtime_adapters`` nowhere, so no cycle is created either way.
from . import runtime_adapters


# Byte caps for the on-attach contract. NF-2026-00281-V2 added the manager-role
# section to the shared POLICY (rendered into all three providers) and a role
# lead-in to CLAUDE_MANAGER_PREAMBLE without cutting any existing protocol rule.
# The pre-role caps (4100 / 5200) left only 9 bytes of canonical and 8 bytes of
# CLAUDE.md headroom, so the role could not fit under them. Nothing was removed;
# the caps are raised to sit just above the measured rendered output.
#
# NF-2026-00294 then added the shared "Multicore by default:" principle to the
# same canonical POLICY (so AGENTS.md, CLAUDE.md and copilot all carry it, not
# just the Claude preamble). Again nothing was cut -- the principle is five new
# lines appended before "Stop at Codex review."; every prior rule renders
# verbatim. The render measured before/after:
#   canonical 5484 -> 6211, AGENTS.md 5582 -> 6309, copilot 5604 -> 6331,
#   CLAUDE.md 7338 -> 8065 bytes.
# The caps are raised to sit just above the largest measured output, keeping the
# same modest headroom the role change used (canonical 6300 >= 6211; projection
# 8200 >= CLAUDE.md 8065). No rule was shortened or dropped to fit.
#
# The self-hosting break-glass authority adds two manager-role lines. The caps
# move with that measured policy growth; the renderer still enforces a bounded
# contract and all provider projections remain covered by tests.
#
# Audit startup-5 / worker_prompt-5 / source_graph-8 (2026-09-08) then removed
# text instead of raising the caps: the Claude preamble no longer restates the
# three "Manager role:" sentences (they were rendered twice per CLAUDE.md, once
# with 'different vendor' and once with 'second vendor'), the Order section is
# split into a manager and a worker sequence so the manager is no longer told
# to validate receipts nothing injects on its route, and the model-typed
# workflow_stage / refresh rules gave way to one server-inference line. The
# caps stay at 7200 / 9200 and the render sits well under them.
#
# Audit edit-1 (2026-09-08) added the mandatory "Semantic edit" section the same
# way -- by absorbing, not by raising a cap. The two Source Graph lines it
# supersedes were removed in the same change, so the measured render moved
# canonical 6924 -> 7163, AGENTS.md 7022 -> 7261, copilot 7044 -> 7283 and
# CLAUDE.md 8226 -> 8706 (that one also carries the manager's own correction
# rule) against unchanged caps of 7200 / 9200. Canonical headroom is now 37
# bytes: the next policy addition has to dedupe something, which is the
# intended pressure.
#
# Audit tools-1 (2026-09-08) added the derived prohibition and the substitution
# table and paid NOTHING for them in this document. Measured, before -> after:
#   canonical 7163 -> 7163, AGENTS.md 7261 -> 7261,
#   CLAUDE.md 8706 -> 8706, copilot 7283 -> 7283.
# Zero, because the canonical sentence's WORDING did not change -- only its
# source did, from a hand-written list to ``RAW_DISCOVERY_DENIED_COMMANDS``. The
# three managed documents and docs/AIWORKHUB_TOOL_USE_POLICY.md therefore need
# no resync for it.
#
# The substitution table itself is +1041 bytes and lives in WORKER_RUNTIME_POLICY
# (5924 -> 6965 measured on this tree), which has no cap of its own and sits
# inside a 160 KiB prompt budget. That placement was measured, not chosen to
# dodge the cap: the three managed FILES never reach a worker at all (a worker
# worktree is a sparse checkout seeded only from the card's declared paths),
# while ``build_worker_prompt`` prepends this prefix for EVERY adapter on the
# single launch path. The manager-seat rows were costed and NOT taken: the
# leanest four-row table measures +365 canonical bytes net of the two lines it
# could honestly absorb (the prohibition itself, and "Use body for an exact
# symbol and bodygrep..."), which against 37 bytes of headroom would need the
# cap at 7550 and would leave CLAUDE.md at 9071 of 9200. That is a cap raise
# bought for a seat that is not the one skipping the tools, so it is reported as
# an owner decision rather than taken here.
#
# Manager correction (2026-09-08), and this one DOES raise the cap, for a reason
# none of the entries above had. Two "every seat" sentences landed in the
# semantic-edit section (+33 measured, canonical 7163 -> 7196), which left 4
# bytes. Four bytes was survivable while the document was hand-written. It is
# not survivable now that "Audit tools-1" made the prohibition DERIVED: the
# canonical grows whenever ``RAW_DISCOVERY_DENIED_COMMANDS`` grows, and a
# realistic seventh command costs about 9 bytes there. So adding one word to the
# enforcement vocabulary would have raised ValueError inside render_canonical --
# in production, at import of the projection, not in a review.
#
# That is a coupling this file did not have before, and a cap has to leave room
# for the thing it is now coupled to. 7300 is the smallest raise that admits
# more than one such command; ``test_the_canonical_has_room_for_the_vocabulary_
# it_is_derived_from`` measures it against the real tuple rather than trusting
# this comment. Nothing was absorbed to pay for it because nothing here is
# redundant -- the prose cost 33 bytes and the other 71 are headroom bought
# deliberately, which is the honest description of it.
CANONICAL_MAX_BYTES = 7300
PROJECTION_MAX_BYTES = 9200
START = "<!-- AIWORKHUB_TOOL_USE_POLICY_START -->"
END = "<!-- AIWORKHUB_TOOL_USE_POLICY_END -->"
# Annotated with the Literal it is built from (a forward reference, resolved
# lazily by ``from __future__ import annotations``): iterating a bare tuple of
# str made every render_projection(provider) call in this module an arg-type
# error, and the union rule scan would have added another.
PROVIDERS: tuple[Provider, ...] = ("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md")

Provider = Literal["AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md"]


# ---------------------------------------------------------------------------
# Derived raw-discovery vocabulary.
#
# There were three hand-written copies of the same list: the enforced tuple in
# ``runtime_adapters``, the canonical POLICY sentence ("Never use grep, rg,
# find, tree, ...") and the worker prompt sentence ("Raw Grep, Glob, grep, rg,
# find and tree discovery are provider-blocked."). They agreed by luck. Both
# rendered copies are now built from the tuples, so adding a command to
# ``RAW_DISCOVERY_DENIED_COMMANDS`` changes what every seat is told.
#
# The canonical sentence keeps its exact previous wording and byte count -- the
# derivation is a change of SOURCE, not of text -- so the three managed
# documents and docs/AIWORKHUB_TOOL_USE_POLICY.md need no resync for it.
RAW_DISCOVERY_COMMANDS = ", ".join(runtime_adapters.RAW_DISCOVERY_DENIED_COMMANDS)
RAW_DISCOVERY_NATIVE_TOOLS = ", ".join(
    runtime_adapters.CLAUDE_RAW_DISCOVERY_TOOL_DENIES
)


CLAUDE_MANAGER_PREAMBLE = """Claude Code manager role (read before the protocol below): every direct Claude chat holds this seat.
Claude Code manager startup (mandatory when AIWorkHub MCP is available):
- Before Read, Grep, Glob, Bash or filesystem discovery, call aiworkhub_manager_bootstrap.
- Continue only when repository identity and manager route are verified.
- The verified bootstrap/repository_current repository outranks host cwd, workspace_roots and environment_context; on mismatch stop before filesystem access, switch/reload the route and never inspect the hinted repository.
- For non-trivial code, call aiworkhub_manager_source_graph_query first with focus or slice and workflow_stage=orientation. Re-query when the symbol, boundary, hypothesis or stage changes.
- Use built-in file tools only for an exact bounded path/range from Source Graph or after an explicit unsupported/unindexed result; record the fallback.
- Make the seat's small corrections with scripts/manager_semantic_edit.py --path --start --end (replacement on stdin): it replaces one hash-verified line range and never rewrites a file. The manager does not correct by whole-string rewrite.
- If bootstrap or required Source Graph is unavailable, report the MCP problem instead of silently bypassing AIWorkHub.
- Direct Claude chats use manager tools; launched task workers use worker tools.
- The seat's duties are the "Manager role:" rules of the policy below; they are stated once, there.
"""


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Structured policy source used by every provider projection."""

    title: str
    role: tuple[str, ...]
    manager_order: tuple[str, ...]
    worker_order: tuple[str, ...]
    adaptive: tuple[str, ...]
    source_graph: tuple[str, ...]
    semantic_edit: tuple[str, ...]
    validation: tuple[str, ...]
    session: tuple[str, ...]
    context_graph: tuple[str, ...]
    memory: tuple[str, ...]
    kb: tuple[str, ...]
    multicore: tuple[str, ...]
    finish: str


POLICY = ToolPolicy(
    title="AIWorkHub MCP tool-use policy",
    role=(
        "The manager does not write code: it runs the project with the owner, distributes work to workers by difficulty and cost, and reviews what returns; small precise corrections are allowed, building features is the workers' job.",
        "Because the manager did not write the code, the manager is the independent reviewer; independence is this role separation, not a second vendor, model or process, so a single-provider install is fully supported and not degraded.",
        "Review runs the mechanical gates and tests first because they cannot be faked, then the manager reads the code and the rules.",
        "Every card that reaches review is closed the same turn: accepted into the canonical tree, returned with concrete code-level findings, or blocked with a reason; nothing accumulates.",
        "Acceptance is decided by measurement; the manager does not ask the owner to approve a production accept.",
        "Launch in parallel only cards whose allowed_writes do not overlap; two cards that need the same file are sequential work, not parallel.",
        "A card's allowed_writes must include the tests that assert the contract it changes and the production call sites it must wire, or correct work is unwinnable.",
        "Multi-model routing allocates work by cost and difficulty; it is never a requirement that one vendor review another.",
        "Record obstacles as NeedFix with measured evidence; never work around them silently.",
        "Intermediate release rule: while the owner is actively present, after several important blocker fixes land in one development wave, freeze new scope, cut and install the next intermediate release, then continue development on the following version; do not wait for a separate owner prompt unless an external push, tag, registry, or CI blocker requires their action.",
        "Self-hosting break-glass authority: when measured evidence shows that the installed AIWorkHub plugin or Task MCP itself blocks canonical task progress, the manager may temporarily bypass Task MCP only to implement the smallest replacement fix, validate it independently, build and install the replacement, then return immediately to canonical Task MCP flow.",
        "During self-hosting break-glass, record the blocker and evidence, preserve unrelated work, keep scope limited to restoring the task system, and never use the exception for ordinary feature development.",
    ),
    # Two sequences, one per seat. The receipt steps are worker-side facts
    # (process_launcher builds the injected bundle and its receipt for launched
    # workers); nothing injects a receipt on a direct manager chat, so telling
    # the manager to validate one only produced prose claiming it had.
    manager_order=(
        "aiworkhub_manager_source_graph_query",
        "aiworkhub_manager_session_current_state",
        "aiworkhub_manager_ai_memory_search",
        "aiworkhub_manager_kb_search/get/related",
        "aiworkhub_manager_context_graph_search, aiworkhub_manager_context_graph_range and aiworkhub_manager_context_graph_related when enabled",
        "launch, review and close cards through the manager task tools",
    ),
    worker_order=(
        "the coordinator records the injected bundle receipt; do not print it",
        "aiworkhub_worker_source_graph_query",
        "aiworkhub_worker_session_current_state",
        "aiworkhub_worker_ai_memory_search",
        "aiworkhub_worker_kb_search/get/related",
        "never Context Graph",
        "execute exact card action and validation",
    ),
    adaptive=(
        "Role-specific AIWorkHub MCP tools are mandatory for managers and workers; legacy AITools scripts/databases are not model interfaces.",
        "Verified repo and repo_id outrank cwd, workspace_roots, environment_context and chat prose; on mismatch stop before filesystem access and switch/reload the route, never inspect the hinted repo as fallback.",
        "Task MCP receipt is always required; Source Graph is required for code tasks.",
        "Session Manager, AI Memory and KB run only when the card requests them or the task is non-trivial.",
        "Workers submit durable context changes only through the session/AI Memory/KB write-intent tools; a verified manager accepts or rejects each intent before canonical apply. Never write context databases directly.",
        "Do not make empty irrelevant calls to satisfy ceremony.",
    ),
    source_graph=(
        "When source_graph_required is true, stop if its bundle is unavailable, empty, stale or unacknowledged.",
        f"Never use {RAW_DISCOVERY_COMMANDS}, broad cat/sed or recursive listing while Source Graph can index/process the target.",
        "A bounded exact-target fallback is allowed only after Source Graph reports that target unsupported or unindexed; record that reason.",
        "Re-query whenever the active symbol, dependency boundary, failure hypothesis, edit scope or validation target materially changes.",
        "workflow_stage is inferred by the server; pass it only to override.",
        "Start with focus/slice; escalate from returned evidence to context/calls/trace, impact, testmap/coverage and then a typed bundle only when needed.",
        "Use body for an exact symbol and bodygrep for indexed literal/body text.",
        "Final HMAC-authenticated MCP audit ledger receipts distinguish injected, live, zero-hit and cache-hit calls plus modes and fallbacks; one preflight query is not continuous use.",
    ),
    # Audit edit-1 (2026-09-08, 659 parseable worker runs): about half of every
    # measured edit bypassed the semantic editor -- codex ran raw apply_patch
    # 1,139 times against 577 semantic applies, claude raw Edit 505 + Write 71
    # against 444 -- and 2,396 of codex's 2,901 prepares (83%, 24.9 MB of
    # fragment bytes, 93% on a file the run had already read) were never
    # applied, i.e. prepare was being used as a reader. The semantic editor is
    # one of the two measured token pillars (~60x against whole-file rewrite),
    # so the mandate is stated here, once, and named exceptions replace taste.
    # Nothing was dropped to fit it: this section absorbs the two Source Graph
    # lines it supersedes ("After Source Graph finds an exact target, prefer
    # body/file preview; otherwise use a bounded read and never reread an
    # unchanged range." and "For edits prefer
    # aiworkhub_worker_semantic_edit_prepare/apply with the smallest verified
    # range."), so CANONICAL_MAX_BYTES did not move.
    semantic_edit=(
        "Every seat changes an existing file with aiworkhub_worker_semantic_edit_prepare then _apply, or the manager pair, on the smallest verified range; a whole-file rewrite is not an editing strategy.",
        "Exceptions: a new file, a change spanning most of a file, or an adapter without these tools; record which one applies, never a silent raw edit.",
        "prepare is an edit step, not a reader: read with body/file preview, otherwise use a bounded read and never reread an unchanged range.",
    ),
    validation=(
        "Exact validation/build/test commands named by the card are allowed.",
        "Exact known-path reads from the card or Source Graph are allowed; they are not broad discovery.",
    ),
    session=(
        "Recover current state before non-trivial assumptions and preserve the returned session identity in the handoff.",
        "Never store secrets or fabricate session evidence.",
    ),
    context_graph=(
        "Manager-only when enabled: search for non-trivial continuation, compaction/handoff recovery or prior-conversation facts; use range/related only from returned evidence.",
        "Workers never query or write Context Graph; durable context uses Session/AI Memory/KB write intents.",
        "Disabled or zero-hit is not failure; no empty ceremonial calls.",
    ),
    memory=(
        "After session recovery, issue one bounded task-specific query.",
        "Reuse returned durable decisions/lessons.",
        "Do not query legacy memory files directly.",
    ),
    kb=(
        "Query authoritative project contracts/docs for unresolved factual context and preserve source identity.",
        "After a zero hit, do not repeat the query unless task scope changes.",
    ),
    multicore=(
        "AIWorkHub is written for multiple cores: work that is independent per item runs across cores by default; a sequential path is the exception, and the code says why in a comment.",
        "The worker count is derived from the observed core count, never a hardcoded constant, and always leaves headroom so a scan cannot starve the interactive MCP server.",
        "Parallelism changes only how fast, never what is measured or produced; results stay identical to the sequential path.",
        "Threads for IO-bound work that releases the GIL, processes for CPU-bound work, chosen from a measurement, not a rule of thumb.",
        "A path left sequential after measurement is a valid outcome; the recorded measurement is what justifies it.",
    ),
    finish="Stop at Codex review.",
)


# B833: informational cross-reference only. Deliberately NOT part of
# ``POLICY`` -- adding it there would change ``render_canonical()`` /
# ``render_projection()`` byte output, which is pinned exactly by
# the pinned agent-tool activation evaluation. Kept here so callers/tests
# have one canonical place to
# check the dynamic worker MCP tool names against the policy module without
# hand-duplicating the list.
WORKER_MCP_TOOL_NAMES: tuple[str, ...] = (
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_semantic_edit_prepare",
    "aiworkhub_worker_semantic_edit_apply",
    "aiworkhub_worker_semantic_edit_exception_declare",
    "aiworkhub_worker_session_current_state",
    "aiworkhub_worker_ai_memory_search",
    "aiworkhub_worker_ai_memory_get",
    "aiworkhub_worker_ai_memory_related",
    "aiworkhub_worker_kb_search",
    "aiworkhub_worker_kb_get",
    "aiworkhub_worker_kb_related",
    "aiworkhub_worker_session_write_intent",
    "aiworkhub_worker_ai_memory_write_intent",
    "aiworkhub_worker_kb_write_intent",
    "aiworkhub_worker_quality_review_packet_read",
    "aiworkhub_worker_quality_review_submit",
    "aiworkhub_worker_validation_run",
    "aiworkhub_worker_validation_output_page",
    "aiworkhub_worker_exit_preflight",
)

MANAGER_CONTEXT_GRAPH_TOOL_NAMES: tuple[str, ...] = (
    "aiworkhub_manager_context_graph_search",
    "aiworkhub_manager_context_graph_range",
    "aiworkhub_manager_context_graph_related",
)

# The worker MCP server registers under this name (worker_ai_tools_mcp.SERVER_NAME;
# runtime_adapters._WORKER builds the same "mcp__<server>__<tool>" prefix for
# --allowedTools). A Claude CLI host defers MCP schemas and makes the model
# load them through ToolSearch; audit worker_prompt-4 measured 347 ToolSearch
# calls in 192 claude runs, half of them keyword searches and 26% misses, all
# re-discovering this static list. The exact one-shot select string is
# rendered only for claude_cli so Codex is never told to call ToolSearch.
WORKER_MCP_SERVER_NAME = "aiworkhub_worker_ai_tools"
# The exact schemas a build worker needs in hand before its first action:
# discovery, the two semantic-edit steps, the bounded validation runner and the
# exit rehearsal. Preloading the last two is what keeps a worker from
# rediscovering them mid-run and falling back to raw Bash validation.
WORKER_CLAUDE_PRELOADED_TOOLS: tuple[str, ...] = (
    *WORKER_MCP_TOOL_NAMES[:3],
    # The declaration channel is preloaded for the same reason the validation
    # runner is: a worker about to take a raw fallback is exactly the worker who
    # will not think to search the deferred schemas for a tool that records one.
    # A fallback channel undiscoverable at the moment of fallback is not one.
    "aiworkhub_worker_semantic_edit_exception_declare",
    "aiworkhub_worker_validation_run",
    "aiworkhub_worker_exit_preflight",
)
WORKER_CLAUDE_TOOL_SCHEMA_QUERY = "select:" + ",".join(
    f"mcp__{WORKER_MCP_SERVER_NAME}__{name}" for name in WORKER_CLAUDE_PRELOADED_TOOLS
)
_CLAUDE_TOOL_SCHEMA_BLOCK = (
    "CLAUDE_TOOL_SCHEMAS: a Claude CLI host defers MCP tool schemas. Load the "
    "required schemas with exactly one ToolSearch call before any other tool "
    f'call, query "{WORKER_CLAUDE_TOOL_SCHEMA_QUERY}"\n'
    "(select: is exact; never search schemas by keyword)."
)


# ---------------------------------------------------------------------------
# The substitution table.
#
# The policy forbade and never substituted.  A model told "never grep" with no
# named replacement reaches for cat, sed, a wider read, or guesses -- and for
# six of the nine supported adapters (every vscode_lm route, codex_cli,
# grok_kilo_cli and deepseek_manual) there is NO argv-level deny behind the
# words, so the text is the whole of the enforcement.
#
# Both halves of each row are derived: the forbidden surface from the
# ``runtime_adapters`` deny tuples, the replacement from ``WORKER_MCP_TOOL_NAMES``
# via ``_WORKER_TOOL``, which raises at import if a tool is renamed.  A renamed
# tool therefore cannot leave a dangling instruction pointing at a name the
# server no longer registers.
#
# The table is rendered into WORKER_RUNTIME_POLICY, not into the canonical
# POLICY, and that is a measured choice rather than a budget dodge:
#   * the three managed instruction FILES are never present in a worker's
#     worktree -- ``worker_workspace._prepare_sparse_worktree`` seeds only the
#     card's declared paths -- so AGENTS.md/CLAUDE.md/copilot-instructions.md
#     reach a manager chat in the canonical root and reach no worker at all;
#   * every launched worker on every adapter DOES receive this string:
#     ``build_worker_prompt`` prepends ``render_worker_runtime_policy()`` on the
#     single launch path, before the adapter branch that splits the in-process
#     vscode_lm bridge from the subprocess CLIs;
#   * the canonical POLICY has 37 bytes of headroom and the leanest useful
#     four-row table measures +365 bytes net of everything it can honestly
#     absorb, so putting it there would buy the manager rows with a cap raise.
# The manager-seat rows are therefore reported as a costed option, not taken.
_WORKER_TOOL: dict[str, str] = {
    name[len("aiworkhub_worker_"):]: name for name in WORKER_MCP_TOOL_NAMES
}

WORKER_SUBSTITUTIONS: tuple[tuple[str, str], ...] = (
    (
        f"raw search ({RAW_DISCOVERY_NATIVE_TOOLS}, {RAW_DISCOVERY_COMMANDS},"
        " broad cat/sed, recursive listing)",
        f"{_WORKER_TOOL['source_graph_query']} -- focus/slice to locate, then"
        " body for an exact symbol or bodygrep for indexed literal text",
    ),
    (
        "a whole-file rewrite, or a raw apply_patch/Edit/Write over a range"
        " these tools can take",
        f"{_WORKER_TOOL['semantic_edit_prepare']} then"
        f" {_WORKER_TOOL['semantic_edit_apply']}",
    ),
    (
        "retyping or re-deriving a validation command, or typing pytest/ruff/"
        "mypy into Bash",
        _WORKER_TOOL["validation_run"],
    ),
    (
        "an unbounded file read, or rereading an unchanged range",
        "one Source Graph body/file preview, then one bounded range you reuse",
    ),
    (
        "calling your own work finished",
        _WORKER_TOOL["exit_preflight"],
    ),
)

WORKER_SUBSTITUTION_BLOCK = "\n".join(
    [
        "USE_THE_AIWORKHUB_TOOL_INSTEAD (forbidden surface -> the tool that does that job):",
        *[f"- {surface} -> {tool}." for surface, tool in WORKER_SUBSTITUTIONS],
        "- When the named tool is genuinely unavailable, say which one and why in"
        " the final message, then make the smallest bounded fallback. A fallback"
        " nobody named is an unrecorded one.",
    ]
)


# ---------------------------------------------------------------------------
# Enforcement honesty, per adapter.
#
# Measured against ``runtime_adapters.adapter_enforces_raw_discovery_denies``
# on 2026-09-08: 3 of 9 supported adapters carry an argv-level tool deny
# (claude_cli, deepseek_copilot_cli, glm_copilot_cli).  The other six --
# codex_cli, grok_kilo_cli, deepseek_manual and all three vscode_lm routes --
# have none, and for them this text is the entire control.  Saying
# "provider-blocked" to a seat where nothing blocks it teaches the model that
# the rule is somebody else's problem; saying it to a seat where the argv
# already refuses spends bytes repeating a refusal it will meet anyway.  So the
# sentence is rendered from the capability, and only where it is load-bearing.
_ENFORCEMENT_NOTICE = (
    "RAW_DISCOVERY_ENFORCEMENT: this transport ({adapter_id}) passes no"
    " tool-deny to your provider ({reason}), so nothing mechanically stops the"
    " commands above -- this instruction is the whole of the rule. The"
    " HMAC-authenticated MCP audit ledger the coordinator verifies after you"
    " exit is what records whether you kept it."
)


WORKER_RUNTIME_POLICY = f"""You are the sole worker for one exact AIWorkHub task in an isolated worktree.

The coordinator already claimed the task. Do not run taskctl lifecycle commands,
do not commit, and do not modify .git. Work only on the task contract. The
coordinator will independently enforce allowed_writes, rerun validation, promote
accepted files, and request review after your process exits successfully.

Read every read_first path before editing. Create the required evidence, then
run validation ONLY through aiworkhub_worker_validation_run: it resolves the
card's own declared command for you, executes it in this sandbox and returns the
returncode, failure class, pytest short-summary lines and a bounded tail with
the full log addressable by sha256. Never retype a validation command and never
type pytest, ruff or mypy into Bash. On some transports Bash refuses those
outright; on the rest nothing stops you, and typing one anyway produces an
unreceipted run the supervisor cannot read -- so the result does not count
either way. Re-running the tool on unchanged bytes returns the cached receipt;
pass force=true to re-execute.
Never install, download, unpack, vendor, or bootstrap validation dependencies
inside the worker sandbox. When a declared validator is unavailable the tool
reports failure_class tool_unavailable: name it in the final message and
continue no further; the coordinator-side supervisor will still run the
canonical validation after exit.
Never use git add -A or git add . and never touch paths outside allowed_writes.

SANDBOX_VALIDATION_FACTS: your worktree is a sparse checkout, so a declared
.venv/bin/python does not exist inside it; the validation tool already resolves
it, and $AIWORKHUB_CANONICAL_PYTHON, $AIWORKHUB_CANONICAL_RUFF and
$AIWORKHUB_CANONICAL_MYPY are resolved absolute paths for anything else;
never probe them first (no echo, command -v, which, ls or --version turn).
Tool caches and pytest --tb=short are already configured, so never pipe
validation through tail or head and never paste a raw log into your reasoning.
chmod/chown/utime are denied everywhere by
sandbox policy, so a test or fixture that needs them cannot run here: report
exactly which tests were blocked with the prefix
validation_unsupported_in_sandbox: and never stub a denied call to claim a
pass; the coordinator's canonical validation decides those tests after exit.

{WORKER_SUBSTITUTION_BLOCK}

MANDATORY_AIWORKHUB_TOOLS:
- For code discovery call aiworkhub_worker_source_graph_query first and call it
  again whenever you need a new symbol, dependency, call path, control-flow,
  configuration, or file target. Initial injected context is startup material,
  not a substitute for live Source Graph use. Raw {RAW_DISCOVERY_NATIVE_TOOLS},
  {RAW_DISCOVERY_COMMANDS} and recursive-listing discovery are denied for this
  run; the line above names what to call instead.
- Source Graph `target` is an optional exact path filter, never a copy of the
  semantic `query`. Omit `target` unless the task contract or worker MCP
  receipt explicitly declares that exact path as an allowed source target.
- Prefer Source Graph body/file previews after discovery. If an exact provider
  file read is still necessary, request one bounded range and reuse it instead
  of rereading an unchanged identical range.
- SEMANTIC_EDIT_IS_MANDATORY: change an existing file with
  aiworkhub_worker_semantic_edit_prepare on the smallest Source Graph line
  range, then aiworkhub_worker_semantic_edit_apply with replacement code only.
  A whole-file rewrite of an existing file is not an editing strategy, and
  neither is a raw apply_patch, Edit or Write over a range these tools can
  take. The only exceptions are creating a NEW file, a change that genuinely
  spans most of a file, and a provider whose adapter does not expose these two
  tools; in those cases make the smallest possible bounded edit and say which
  exception applied in the final message. The local applier verifies the
  full-file preimage and fragment hash before mutating the isolated worktree.
- prepare is an EDIT step, not a reader. Never prepare a range you are not
  about to change: read code with Source Graph body/file. A prepare for a range
  this server already delivered comes back hash-only (fragment_omitted) -- the
  hashes apply needs, and no text to read.
- Executed, non-degraded Session Manager, AI Memory and KB sections in the
  trusted injected bundle are already canonical queries. Acknowledge and reuse
  them; call the corresponding live tool only when its section is absent or
  degraded, or when a new unresolved fact makes another query relevant.
  Never repeat an unchanged zero-hit query as ceremony.
- The coordinator verifies an HMAC-authenticated MCP audit ledger and rejects
  completion when a fresh live Source Graph call is missing. An injected
  receipt or text claim cannot satisfy this execution-time requirement.
- If Source Graph reports an exact target unsupported/unindexed, stop and
  report that target. Only a new coordinator-authorized fallback card may use
  raw discovery for it.
- Before your final message call aiworkhub_worker_exit_preflight once. It
  re-runs the coordinator's own required-output, residual-contract and live
  MCP-call checks read-only and names each failure it would otherwise produce
  after you exit. It never marks anything satisfied, so repair what it lists
  rather than reporting it.

Your final message must be at most 12 lines: what changed and why, plus any
blocked or missing validator. The coordinator computes changed paths and reruns
validation itself, so do not list files or paste test output."""


def raw_discovery_enforcement(adapter_id: str | None) -> dict[str, Any]:
    """What this transport can actually refuse, and what only the text holds.

    ``enforced`` is read from ``runtime_adapters``, never asserted here, so a
    newly added adapter defaults to "does not enforce" and can never inherit an
    enforcement claim nobody verified.  ``adapter_id`` of ``None`` means the
    caller did not say which transport this is, and an unknown transport is
    treated exactly like an unenforcing one -- the honest direction.
    """

    known = bool(adapter_id) and adapter_id in runtime_adapters.SUPPORTED_ADAPTERS
    # Two mechanisms enforce, not one. An argv tool deny is what a subprocess
    # CLI accepts; a closed dispatch surface is what the in-process bridge has,
    # and it is the stronger of the two -- a tool that is never dispatched
    # cannot be called at all. Reading only the argv predicate told the three
    # vscode_lm routes that nothing enforced their rule while their surface
    # served 20 tools, every one aiworkhub_*, with no Grep, Read, Bash, Edit or
    # Write among them. The argv predicate keeps its exact meaning for routing
    # and launch records; this reads the companion that knows about both.
    surface = runtime_adapters.tool_surface_enforcement_fact(str(adapter_id or ""))
    enforced = known and surface["mechanism"] != (
        runtime_adapters.TOOL_SURFACE_MECHANISM_NONE
    )
    detail = surface
    return {
        "adapter_id": str(adapter_id or ""),
        "adapter_known": known,
        "enforced": bool(enforced),
        "reason": str(detail.get("reason") or runtime_adapters.RAW_DISCOVERY_ENFORCEMENT_UNVERIFIED),
        "denied_commands": list(runtime_adapters.RAW_DISCOVERY_DENIED_COMMANDS),
    }


def render_worker_runtime_policy(adapter_id: str | None = None) -> str:
    """Return the canonical stable worker prefix from the policy module.

    With ``adapter_id`` the prefix also tells that seat the truth about its own
    enforcement.  Six of the nine supported adapters pass no tool-deny to the
    provider, and for those the rendered text IS the enforcement; saying so is
    the difference between a rule a model treats as somebody else's problem and
    one it knows it is personally holding.  Where the argv already refuses, no
    bytes are spent repeating a refusal the run will meet anyway.

    The parameter is optional so every existing caller keeps working unchanged;
    a caller that knows the adapter should pass it.
    """

    policy = WORKER_RUNTIME_POLICY
    if adapter_id == "claude_cli":
        policy = f"{policy}\n\n{_CLAUDE_TOOL_SCHEMA_BLOCK}"
    if adapter_id is None:
        return policy
    enforcement = raw_discovery_enforcement(adapter_id)
    if enforcement["enforced"]:
        return policy
    notice = _ENFORCEMENT_NOTICE.format(
        adapter_id=enforcement["adapter_id"] or "unknown",
        reason=enforcement["reason"],
    )
    return f"{policy}\n\n{notice}"


CONTRACT_CLAUSES: dict[str, tuple[str, ...]] = {
    "source_graph_first": (
        "aiworkhub_manager_source_graph_query",
        "aiworkhub_worker_source_graph_query",
    ),
    "continuous_source_graph": ("Re-query", "again whenever"),
    "bounded_fallback": ("bounded exact-target fallback", "bounded range"),
    "semantic_edit": ("aiworkhub_worker_semantic_edit_prepare",),
    "evidence_review": ("HMAC-authenticated MCP audit ledger",),
}


def _lines(policy: ToolPolicy = POLICY) -> list[str]:
    return [
        f"# {policy.title}",
        "Manager role:",
        *[f"- {item}" for item in policy.role],
        "Manager Order:",
        *[f"{idx}. {step}." for idx, step in enumerate(policy.manager_order, start=1)],
        "Worker Order:",
        *[f"{idx}. {step}." for idx, step in enumerate(policy.worker_order, start=1)],
        "Adaptive use:",
        *[f"- {item}" for item in policy.adaptive],
        "Source Graph gate:",
        *[f"- {item}" for item in policy.source_graph],
        "Semantic edit (mandatory):",
        *[f"- {item}" for item in policy.semantic_edit],
        "Exact-command exception:",
        *[f"- {item}" for item in policy.validation],
        "Session Manager:",
        *[f"- {item}" for item in policy.session],
        "Manager Context Graph:",
        *[f"- {item}" for item in policy.context_graph],
        "AI Memory:",
        *[f"- {item}" for item in policy.memory],
        "KB:",
        *[f"- {item}" for item in policy.kb],
        "Multicore by default:",
        *[f"- {item}" for item in policy.multicore],
        policy.finish,
    ]


def _assert_size(label: str, text: str, limit: int) -> None:
    size = len(text.encode("utf-8"))
    if size > limit:
        raise ValueError(f"{label}_too_large:{size}>{limit}")


def render_canonical(policy: ToolPolicy = POLICY) -> str:
    """Render the compact canonical policy, capped by contract."""

    text = "\n".join(_lines(policy)).strip() + "\n"
    _assert_size("canonical_policy", text, CANONICAL_MAX_BYTES)
    return text


def render_projection(provider: Provider, policy: ToolPolicy = POLICY) -> str:
    """Render one deterministic provider projection from the same policy."""

    if provider not in PROVIDERS:
        raise ValueError(f"unsupported_provider:{provider}")
    body = render_canonical(policy)
    provider_preamble = CLAUDE_MANAGER_PREAMBLE if provider == "CLAUDE.md" else ""
    text = f"{START}\nTarget: {provider}\n{provider_preamble}{body}{END}\n"
    _assert_size(f"projection:{provider}", text, PROJECTION_MAX_BYTES)
    return text


def render_all(policy: ToolPolicy = POLICY) -> dict[str, str]:
    return {provider: render_projection(provider, policy) for provider in PROVIDERS}


def inspect_document(text: str) -> dict[str, Any]:
    """Inspect managed markers without mutating owner text."""

    starts = text.count(START)
    ends = text.count(END)
    valid = starts == 1 and ends == 1 and text.find(START) < text.find(END)
    return {
        "start_count": starts,
        "end_count": ends,
        "managed": valid,
        "fail_closed": (starts != ends) or starts > 1 or ends > 1 or (starts == 1 and text.find(START) > text.find(END)),
    }


# Out-of-block policy copies. render_projection places the Claude preamble
# inside the managed block and the replacer rewrites only between the markers,
# so a copy of the block that once lived above the START marker was never
# removed and never detected: CLAUDE.md carried 1,775 B of it in every manager
# turn (audit startup-5). The scan below reads the owner text around the block.
# A verbatim copy is stripped by the apply plan. A copy whose wording drifted is
# reported and the plan fails closed, because choosing between two wordings is
# the owner's call, not the renderer's.
#
# Audit edit-1 (2026-09-08) widened it twice, because the first version scanned
# each document against its own rendered block only:
#   * the rule set is now every line ANY managed projection renders, so a copy
#     of the CLAUDE.md manager preamble sitting in AGENTS.md is a copy -- that
#     is exactly the 1,018 B "Kilo manager startup (copied from CLAUDE.md...)"
#     duplicate that survived every --check (AGENTS.md carried 1,975 B above
#     its marker, of which that block was all but the repository's own notes),
#     since AGENTS.md's own projection carries no preamble and the lines
#     matched nothing in it;
#   * a renamed copy is caught by similarity, not only by a shared 40-character
#     prefix. That duplicate renamed "Claude" to "Kilo" inside the first nine
#     characters of two of its lines, so the prefix test saw owner prose.
# Measured on this repository's own AGENTS.md owner text: the copied lines score
# 0.79 and 0.95 against their rendered original, while the highest-scoring real
# owner line of >= 40 characters reaches 0.53. The threshold sits between them.
# The similarity pass runs only on lines of >= _DRIFT_PREFIX_CHARS that are not
# already verbatim or prefix matches, and costs ~0.6 ms per such line against
# the ~90 rendered rules; the three managed documents carry ~20 owner lines
# each, and the full --check measures 0.09 s wall. A character-multiset
# prefilter was measured first and rejected: it pruned almost nothing, because
# ordinary English prose scores 0.75-0.79 on letter counts alone.
_DRIFT_PREFIX_CHARS = 40
_DRIFT_MIN_RATIO = 0.75


def managed_policy_rule_lines(policy: ToolPolicy = POLICY) -> frozenset[str]:
    """Every rule line any managed projection renders.

    A duplicate is a duplicate wherever it is parked, so the out-of-block scan
    compares against the union of all providers (the shared canonical body plus
    every provider preamble), never against one document's own block.
    """

    return frozenset(
        line
        for provider in PROVIDERS
        for line in render_projection(provider, policy).splitlines()
        if line and line not in (START, END)
    )


@dataclass(frozen=True, slots=True)
class OutsideScan:
    """Policy lines found outside the managed block of one owner document."""

    verbatim: tuple[str, ...]
    drifted: tuple[str, ...]
    before: str
    after: str

    @property
    def fail_closed(self) -> bool:
        return bool(self.drifted)


def _is_section_label(line: str) -> bool:
    # "KB:" or "Order:" alone is ordinary owner vocabulary; a label counts as a
    # policy copy only when it travels with rule lines.
    return (
        line.endswith(":")
        and not line.startswith(("- ", "Target: ", "# "))
        and not line[:1].isdigit()
    )


def _classify(line: str, rules: set[str], prefixes: set[str]) -> str:
    if line in rules:
        return "verbatim"
    if len(line) < _DRIFT_PREFIX_CHARS:
        # Too short to tell a copy from ordinary owner vocabulary.
        return "owner"
    if line[:_DRIFT_PREFIX_CHARS] in prefixes:
        return "drifted"
    if difflib.get_close_matches(line, rules, n=1, cutoff=_DRIFT_MIN_RATIO):
        # A copy that was renamed early enough to keep no shared prefix.
        return "drifted"
    return "owner"


def _scan_segment(
    segment: str, rules: set[str], prefixes: set[str]
) -> tuple[list[str], list[str], str, bool]:
    lines = segment.splitlines(keepends=True)
    kinds = [_classify(line.rstrip("\r\n"), rules, prefixes) for line in lines]
    verbatim: list[str] = []
    drifted: list[str] = []
    drop: set[int] = set()
    index = 0
    while index < len(lines):
        if kinds[index] == "owner":
            index += 1
            continue
        end = index
        while end < len(lines) and kinds[end] != "owner":
            end += 1
        run = range(index, end)
        if any(not _is_section_label(lines[i].rstrip("\r\n")) for i in run):
            for i in run:
                if kinds[i] == "verbatim":
                    verbatim.append(lines[i].rstrip("\r\n"))
                    drop.add(i)
                else:
                    drifted.append(lines[i].rstrip("\r\n"))
        index = end
    kept = "".join(line for i, line in enumerate(lines) if i not in drop)
    return verbatim, drifted, kept, bool(drop)


def scan_outside_block(
    before: str, after: str, block: str, policy: ToolPolicy = POLICY
) -> OutsideScan:
    """Find policy lines outside the managed block; strip the verbatim ones.

    ``before``/``after`` are the owner text around the block (the whole text as
    ``before`` when there is no block yet); ``block`` is the rendered
    projection. Rule lines are this block's plus every other managed
    projection's, so a rule copied out of a different provider's block is still
    a copy. Only complete lines count, compared byte-for-byte after the line
    ending. A line that is not verbatim but shares its first
    ``_DRIFT_PREFIX_CHARS`` characters with a rendered rule, or matches one at
    ``_DRIFT_MIN_RATIO`` similarity, is a drifted copy: nothing is stripped
    then and ``fail_closed`` is set, because the outside copy differs from the
    rendered text and choosing between two wordings is the owner's call.
    """

    rules = {line for line in block.splitlines() if line and line not in (START, END)}
    rules |= managed_policy_rule_lines(policy)
    prefixes = {line[:_DRIFT_PREFIX_CHARS] for line in rules if len(line) >= _DRIFT_PREFIX_CHARS}
    verbatim_before, drifted_before, kept_before, dropped_before = _scan_segment(before, rules, prefixes)
    verbatim_after, drifted_after, kept_after, dropped_after = _scan_segment(after, rules, prefixes)
    drifted = (*drifted_before, *drifted_after)
    if drifted:
        return OutsideScan(
            verbatim=(*verbatim_before, *verbatim_after),
            drifted=drifted,
            before=before,
            after=after,
        )
    if dropped_before:
        kept_before = "" if not kept_before.strip() else re.sub(r"\n{3,}\Z", "\n\n", kept_before)
    if dropped_after:
        kept_after = "\n" if not kept_after.strip() else re.sub(r"\A\n{3,}", "\n\n", kept_after)
    return OutsideScan(
        verbatim=(*verbatim_before, *verbatim_after),
        drifted=(),
        before=kept_before,
        after=kept_after,
    )


def _split_around_block(text: str) -> tuple[str, str]:
    """Return the owner text before and after a well-formed managed block."""

    start = text.find(START)
    if start < 0:
        return text, ""
    end = text.find(END, start) + len(END)
    return text[:start], text[end:]


def _assemble(before: str, block: str, after: str, *, managed: bool) -> str:
    if managed:
        return before + block.rstrip() + after
    return before.rstrip() + ("\n\n" if before.strip() else "") + block


def build_apply_plan(provider: Provider, owner_text: str, policy: ToolPolicy = POLICY) -> dict[str, Any]:
    """Return a non-mutating managed-block apply plan."""

    block = render_projection(provider, policy)
    inspection = inspect_document(owner_text)
    if inspection["fail_closed"]:
        return {
            "ok": False,
            "reason": "managed_block_marker_corrupt_or_duplicate",
            "provider": provider,
            "current_text": owner_text,
            "planned_text": owner_text,
            "inspection": inspection,
        }
    before, after = _split_around_block(owner_text)
    scan = scan_outside_block(before, after, block, policy)
    inspection = {
        **inspection,
        "outside_verbatim_lines": len(scan.verbatim),
        "outside_drifted_lines": len(scan.drifted),
    }
    if scan.fail_closed:
        return {
            "ok": False,
            "reason": "policy_text_drifted_outside_managed_block",
            "provider": provider,
            "current_text": owner_text,
            "planned_text": owner_text,
            "inspection": inspection,
            "drifted_lines": list(scan.drifted),
        }
    planned = _assemble(scan.before, block, scan.after, managed=bool(inspection["managed"]))
    return {
        "ok": True,
        "reason": "replace_managed_block" if inspection["managed"] else "append_managed_block",
        "provider": provider,
        "current_text": owner_text,
        "planned_text": planned,
        "inspection": inspection,
        "stripped_lines": list(scan.verbatim),
    }


def diff_projection(provider: Provider, owner_text: str, policy: ToolPolicy = POLICY) -> str:
    """Return a unified diff for the inert apply plan."""

    plan = build_apply_plan(provider, owner_text, policy)
    return "".join(
        difflib.unified_diff(
            owner_text.splitlines(keepends=True),
            str(plan["planned_text"]).splitlines(keepends=True),
            fromfile=f"a/{provider}",
            tofile=f"b/{provider}",
        )
    )


__all__ = [
    "CANONICAL_MAX_BYTES",
    "CLAUDE_MANAGER_PREAMBLE",
    "END",
    "MANAGER_CONTEXT_GRAPH_TOOL_NAMES",
    "OutsideScan",
    "POLICY",
    "PROJECTION_MAX_BYTES",
    "PROVIDERS",
    "RAW_DISCOVERY_COMMANDS",
    "RAW_DISCOVERY_NATIVE_TOOLS",
    "START",
    "WORKER_CLAUDE_TOOL_SCHEMA_QUERY",
    "WORKER_MCP_SERVER_NAME",
    "WORKER_MCP_TOOL_NAMES",
    "WORKER_RUNTIME_POLICY",
    "WORKER_SUBSTITUTIONS",
    "WORKER_SUBSTITUTION_BLOCK",
    "ToolPolicy",
    "build_apply_plan",
    "diff_projection",
    "inspect_document",
    "managed_policy_rule_lines",
    "raw_discovery_enforcement",
    "render_all",
    "render_canonical",
    "render_projection",
    "render_worker_runtime_policy",
    "scan_outside_block",
]
