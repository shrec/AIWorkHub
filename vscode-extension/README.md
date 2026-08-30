<div align="center">
  <img src="https://raw.githubusercontent.com/shrec/AIWorkHub/main/vscode-extension/media/aiworkhub-hero.png" alt="AIWorkHub" width="100%">
</div>

# AIWorkHub for VS Code

AIWorkHub is a repository-native control plane for multi-model software
development. It gives every repository an isolated task system, Source Graph,
durable project context, worker runtime and evidence-first review loop.

The extension opens as a retained editor tab and runs one repository-scoped
MCP stdio runtime on the workspace host. It does not open a browser, bind a
port, expose a LAN service or require an AIWorkHub cloud account.

## What's new in 0.10.69 — 2026-08-30

- Worker completion now requires the supervisor-owned context receipt and live
  Source Graph gate; authenticated provider envelopes bind receipts to their
  authority, request and evidence.
- Launch preparation mechanically validates workspace and task inputs, and
  mypy uses the repository's canonical trusted interpreter.
- Diagnostics are compared with the accepted baseline so new regressions are
  separated from existing results.
- Normalized review findings are ingested idempotently, and accepted targets
  close exactly linked NeedFix findings without replaying lifecycle effects.
- Reserved reviewer launch failures reconcile to terminal state instead of
  leaving work pending.
- Process-launcher validation is isolated in a focused, parity-tested module
  whose size ratchet protects the extracted boundary.

## What's new in 0.10.68 — 2026-08-29

- Mandatory review mechanics now belong to the supervisor and durable task
  system: structured reports are ingested automatically, reviewer evidence is
  canonicalized, and correctness, security and code-quality actions run in a
  leased, restart-safe sequence.
- Each reviewer is selected from live workforce policy. Exact terminal receipts
  are verified before acceptance, actionable findings stop the chain, and
  accepted reviewer and target cards are archived automatically.
- The final lifecycle action resolves exactly linked NeedFix records, while a
  target without a linked finding completes idempotently instead of leaving a
  permanent pending action.
- DeepSeek and GLM routes with no authenticated success history are reported as
  launch-eligible but not yet available or selectable; Windows and LM-discovery
  tests now enforce that same provider-readiness truth.

## What's new in 0.10.67

- Nested Landlock validation now authenticates exact request-owned worktree
  authority, and Windows AppContainer supervision owns native process, job,
  termination and cleanup lifecycles without fabricating terminal results.
- Worker capacity adapts to available CPU and affinity limits, reserves capacity
  on multi-CPU hosts, reports the applied ceiling, and constrains nested pools.
- Decomposition previews return approval-required proposals with stable boundary
  identifiers and the large, low-confidence action class.
- A provider-response contract foundation supplies immutable normalized events,
  strict JSON, canonical bytes and digests; callback-store readiness and WAL
  setup failures return bounded categories without leaking operational details.
- Launcher read-efficiency handling moved to a focused, parity-tested module
  guarded by a size ratchet, and manager archives identify the verified provider
  actor while retaining write-gate enforcement.
- Python 3.14 lock-timeout tests isolate their intended descriptor attempts, and
  Source Graph capacity tests verify the effective worker ceiling.

## What's new in 0.10.66

- Bootstrap now gives managers and workers a clear responsibility matrix and
  construction templates; unresolved Session, Memory and KB context is kept as
  a pending intent for bounded follow-up.
- Source Graph traversal fails closed outside verified authority, and scoped
  audit coverage makes the inspected boundary explicit.
- Reviewer preparation has enforceable bounds and replay-context evidence;
  CAS, raw-terminal, launch-ledger and retained-terminal recovery reconcile
  interrupted execution without inventing a terminal result.
- The extension repairs recoverable repository configuration and revives its
  retained dashboard. It uses the trusted interpreter, presents discovered
  model settings, and keeps CaaS corrections bound to their verification.

## What's new in 0.10.65

- Reviewer evidence now keeps prose out of path authority and changed-path scope,
  then seals immutable prose into the digest-bound review packet.
- Legacy Source Graph migration is read-only, and refresh documentation matches
  the tested serialization behavior.
- The Models settings modal retains its visible shell, pending state and prior
  content while repository settings load.
- A fail-closed Windows AppContainer lifecycle foundation adds deterministic
  identity, job ownership and cleanup evidence; it remains gated and is not yet
  wired into native worker launch.
- Authenticated task-template validation and role overrides are sealed into the
  template contract.
- The native Repository Settings dialog now uses content-addressed webview
  assets and an intrinsic layout, avoiding a blank frame while keeping the
  wrapped footer visible and only the settings list scrollable.
- Known limitation: `AIWORKHUB_01065_NF453_GLM_VSCODE_LM_TOOL_LOOP_V5`
  remains unresolved, its candidate was not promoted, and this release makes no
  claim that the GLM VS Code LM tool loop is fixed.

## Architecture at a glance

<div align="center">
  <img src="https://raw.githubusercontent.com/shrec/AIWorkHub/main/docs/assets/aiworkhub-block-diagram.png" alt="AIWorkHub system architecture block diagram" width="100%">
  <br>
  <em>The complete AIWorkHub control plane, execution, evidence and improvement loop.</em>
</div>

<div align="center">
  <img src="https://raw.githubusercontent.com/shrec/AIWorkHub/main/docs/assets/aiworkhub-source-graph-architecture.png" alt="AIWorkHub Source Graph architecture" width="100%">
  <br>
  <em>Incremental Source Graph refresh, index, query, semantic-edit and review-overlay paths.</em>
</div>


<div align="center">
  <img src="https://raw.githubusercontent.com/shrec/AIWorkHub/main/docs/assets/demo/aiworkhub-task-review-loop.gif" alt="AIWorkHub task, worker, evidence and review loop" width="100%">
  <br>
  <em>Create a bounded task, launch a model worker, inspect its evidence and accept or rework it.</em>
</div>

## Highlights

- Plan and inspect dependency-aware AI tasks from one operational dashboard.
- Delegate to supported local model adapters and track real terminal outcomes.
- Replace repeated raw-source discovery with a repository Source Graph covering
  34 configurable code, data and documentation families.
- Send focused code fragments through staged semantic edits and let the local
  bridge assemble the hash-bound final envelope without model-side full-file
  regeneration.
- Use 31 bounded source-intelligence modes for symbols, calls, tests, impact,
  complexity, ownership, hotspots, gaps and task-shaped context bundles.
- Preserve continuity through Session Manager, AI Memory and KB.
- Review diffs, tests, logs, artifacts, approval history and deterministic
  Quality Evidence before acceptance.
- Run a changed-file Known Bug Scanner across C/C++/CUDA, Python,
  JavaScript/TypeScript, Go, Java/Kotlin and PHP without treating heuristic
  warnings as proven failures.
- Measure whether workers used Source Graph throughout the task through
  authenticated tool-use receipts and continuous-use telemetry.
- Keep repositories isolated in separate `.aiworkhub/` authorities.
- Run on Linux, macOS, native Windows, WSL and Remote-SSH.

## Operational surface

The retained dashboard combines the task DAG, live worker output, Review
Inbox, callback health, model readiness, tool-use statistics, storage
retention, Source Graph coverage and bounded viewers for logs, sessions,
AI Memory and KB. Settings remain repository-local under `.aiworkhub/`, so a
multi-window installation does not share task or context authority between
repositories.

<div align="center">
  <img src="https://raw.githubusercontent.com/shrec/AIWorkHub/main/docs/assets/screenshots/aiworkhub-self-hosted-dashboard.png" alt="AIWorkHub repository dashboard" width="100%">
  <br>
  <em>Tasks, callback health, source coverage, context stores, preflight and evidence in one retained editor tab.</em>
</div>

## Get started

1. Install from the Marketplace (or install a release VSIX) and open a Git
   repository in VS Code.
2. Run **AIWorkHub: Open Dashboard**.
3. Select the repository when using a multi-root workspace.
4. Choose **Initialize AIWorkHub** on first use.
5. Open a new Codex, Claude or MCP-capable chat after registration so the new
   runtime tools are discovered by that chat process.

Initialization is explicit and idempotent. It creates repository-local state
only under `.aiworkhub/` and starts the first Source Graph index in the
background.

For Claude Code, initialization also maintains the repository-local
`.mcp.json` server registration and the bounded AIWorkHub block in `CLAUDE.md`.
Open a **new** Claude chat after initialization or an AIWorkHub upgrade. That
direct chat is instructed to bootstrap as the manager, call manager Source
Graph before broad `Read`/`Grep`/`Glob` discovery, and re-query the graph when
its implementation or validation boundary changes. AIWorkHub-launched task
processes use the separate worker tool surface.

## Run your first task

AIWorkHub is designed for a manager chat that delegates bounded work instead
of letting several models edit one checkout without coordination.

Start a new chat after initialization or upgrade and paste:

```text
Use AIWorkHub as manager for the currently bound repository. Call
aiworkhub_manager_bootstrap first; verify repository identity, manager route,
callback, Source Graph and preflight. Do not edit or launch yet. Report what is
ready and what is degraded.
```

Then describe the desired outcome normally. Ask the manager to create bounded
cards and launch only independent, dependency-ready, non-colliding cards in
parallel. The MCP server also presents this lifecycle as a mandatory contract:
creating a task leaves it `pending`; exact claim plus launch establishes
`processing`; workers stop at `review_ready`; callbacks wake the current
verified manager; and only that manager accepts or rejects verified evidence.

1. Check the dashboard header. Repository, MCP, Source Graph and callback
   state should be ready; **Preflight** explains any unavailable optional
   model adapters.
2. Tell the manager what outcome you want. The manager creates a task card
   with an exact objective, acceptance criteria, allowed writes, validation
   commands and dependencies.
3. The manager selects a ready adapter/model and launches the exact card.
   Workers receive repository-scoped Source Graph, Session, Memory and KB
   context and work in an isolated task workspace.
4. Follow **Live Output** or continue other work. Terminal outcomes are durable
   and the originating manager receives a callback when review is required.
5. Open the task in **Review**. Inspect the bounded diff, tests, logs,
   artifacts, tool-use receipts and independent reviewer evidence.
6. **Accept** promotes the verified change and finalizes the task. **Reject**
   records exact feedback and creates a bounded residual rather than silently
   discarding the previous evidence.

Dependency cards remain pending until their prerequisites finish. Collision
checks prevent two active workers from owning overlapping write paths.

See the [complete first-run and manager manual](https://github.com/shrec/AIWorkHub/blob/main/docs/GETTING_STARTED.md)
for copy/paste planning/review prompts, Remote-SSH behavior and recovery after
an interrupted write acknowledgement.

## Models and authentication

AIWorkHub does not proxy credentials or require an AIWorkHub account. It uses
models already authenticated in the corresponding editor or CLI.

| Runner | Typical adapter | Requirement |
| --- | --- | --- |
| Codex | Codex CLI or VS Code Language Model | Existing Codex login or one-time VS Code consent |
| Claude | Claude Code CLI or VS Code Language Model | Existing Claude subscription login or one-time VS Code consent |
| DeepSeek V4 Pro/Flash | VS Code Language Model or Copilot CLI fallback | Provider visible in VS Code; fallback uses its own stored credential |
| GLM 5.2 | VS Code Language Model or Copilot CLI fallback | Provider visible in VS Code; fallback uses its own stored credential |
| Copilot-hosted models | VS Code Language Model | GitHub sign-in and one-time model consent |

The **Preflight** and **Workforce** views report observed availability,
adapter/model identity, outcomes, latency, token evidence and known cost. An
optional adapter being unavailable does not block otherwise ready models.

## Source Graph and context

Source Graph is an incrementally refreshed structural repository index, not a
remote Sourcegraph service. Managers and workers start with low-token `focus`
and `slice` queries, then use calls, trace, impact, test mapping or typed
bundles only when the task needs them. Operations telemetry shows which modes
were requested and executed, returned evidence, workflow stage, latency,
generation and inter-call gaps.

Session Manager stores current state and handoffs; AI Memory stores durable
lessons; KB stores curated project facts; the optional Manager Context Graph
preserves bounded manager transcript evidence. All are repository-local and
have bounded viewers in the dashboard.

## Commands

- **AIWorkHub: Open Dashboard** — open or reveal the retained editor tab.
- **AIWorkHub: Select Repository** — bind the dashboard in a multi-root window.
- **AIWorkHub: Refresh Dashboard** — refresh the current repository snapshot.
- **AIWorkHub: Restart MCP Connection** — replace only AIWorkHub's selected
  repository MCP child.

## Remote development

AIWorkHub is a workspace extension. In Remote-SSH, install it on the remote
extension host; its packaged Python runtime, MCP child and repository state run
beside the remote checkout. No port forwarding is required.

## If the dashboard is not ready

- **Connecting:** use **AIWorkHub: Restart MCP Connection** once and inspect
  the dashboard's last-log row. The extension restarts only its own child.
- **A model is unavailable:** open Preflight, confirm the provider is installed
  and grant the one-time VS Code model consent when prompted.
- **Source Graph is empty:** initialize the repository, enable the required
  language family in Settings and run a refresh.
- **A chat cannot see tools:** open a new chat after installation or upgrade so
  that client performs MCP discovery against the current runtime.
- **Windows upgraded from an old build:** activation automatically migrates
  legacy source/version `PYTHONPATH` registrations to a host-stable packaged
  runtime; no manual `config.toml` edit is required.

## Trust and privacy

- Local stdio transport; no AIWorkHub network listener.
- Read-only and launch-disabled by default.
- Repository-specific state, route identity and audit trail.
- No AIWorkHub telemetry upload of prompts, source, credentials or memories.
- Explicit manager authority for context writes and task acceptance.

Read the full [Getting Started guide](https://github.com/shrec/AIWorkHub/blob/main/docs/GETTING_STARTED.md),
[Architecture](https://github.com/shrec/AIWorkHub/blob/main/docs/ARCHITECTURE.md),
[Source Graph guide](https://github.com/shrec/AIWorkHub/blob/main/docs/SOURCE_GRAPH.md),
[Manager Context Graph guide](https://github.com/shrec/AIWorkHub/blob/main/docs/CONTEXT_GRAPH.md),
[Security Policy](https://github.com/shrec/AIWorkHub/blob/main/SECURITY.md) and
[Product Roadmap](https://github.com/shrec/AIWorkHub/blob/main/docs/PRODUCT_ROADMAP.md).

## Development build

```bash
npm --prefix vscode-extension install
npm --prefix vscode-extension test
npm --prefix vscode-extension run package
code --install-extension vscode-extension/dist/aiworkhub-*.vsix
```

AIWorkHub is open source under the
[MIT License](https://github.com/shrec/AIWorkHub/blob/main/LICENSE).
