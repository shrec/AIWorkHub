<div align="center">
  <img src="https://raw.githubusercontent.com/shrec/AIWorkHub/main/vscode-extension/media/aiworkhub-hero.png" alt="AIWorkHub" width="100%">
</div>

# AIWorkHub for VS Code

**Plan. Delegate. Verify. Remember.**

AIWorkHub is a repository-native control plane for multi-model software
development. It gives every repository an isolated task system, Source Graph,
durable project context, worker runtime and evidence-first review loop.

The extension opens as a retained editor tab and runs one repository-scoped
MCP stdio runtime on the workspace host. It does not open a browser, bind a
port, expose a LAN service or require an AIWorkHub cloud account.

## What's new in 0.11.53

- The bundled semantic reviewer requests hash-matched candidate overlays for
  omitted hunks and still fails closed on missing or stale evidence.
- Bundled task creation checks required outputs; terminal events distinguish
  validation failures from provider timeouts and bound dashboard snapshots.
- Worker-attempt reasoning/context receipts are durable, not proof of
  provider-internal effort or improved quality.
- Full Playbook stage gates, LSP index integration and Muse/OpenCode worker
  qualification remain in progress.

## What's new in 0.11.52

- The wave mini-roadmap now shows the current wave's goals as a live checklist
  joined to each goal's task states.
- The bundled runtime bounds semantic review to a candidate's exact changed
  segments and fails closed on missing or stale changed-segment evidence.
- Source Graph ships a bounded LSP transport with fail-closed definition
  classification over a private workspace. LSP index integration is not
  included in this release.
- Blocked-rework recovery lets a strictly later terminal failure supersede a
  stale retained predecessor (NF-2026-00515).
- The full stage-gated Playbook, reasoning matched to an accepted outcome, and
  Muse/OpenCode worker qualification are still incomplete.

## What's new in 0.11.51

- The bundled runtime exposes repository-bound SDLC case receipts and exact
  task links. Full Playbook stage gates are still under development.
- VS Code LM records the exact reasoning option handed to `sendRequest` and
  the model-reported context capacity, while leaving provider-internal effort
  unknown. Accepted-task outcome metrics now report complete-history coverage.
- OpenCode uses the short `awh` MCP alias; worker recovery and required-output
  handling are hardened. Semantic Review's delta scope and LSP resolution are
  not claimed as complete in this intermediate release.

## What's new in 0.11.50

- The identity strip has a wave mini-roadmap info popup for the current
  in-progress Roadmap wave.
- VS Code LM requests apply reasoning effort only when the model
  declares a selectable control that honors the canonical profile;
  context capacity is recorded and never used to pad the prompt. CLI
  workers in the bundled runtime follow the same APPLIED-only rule.
- Native reviewer retained streams also compact thinking deltas.
- Sparse-worktree VS Code test-asset seeding and the sandbox-safe
  OpenCode config check are repository-only test/manager work, not
  VSIX features.

## What's new in 0.11.49
- This intermediate release combines the staged Windows and OpenCode fixes
  below; the 0.11.45-0.11.48 headings were development notes, not separate
  published releases.
- OpenCode's global AIWorkHub MCP server is registered as `awh` so models with
  a short MCP-name limit can use it without changing the server's identity.
- The bundled runtime includes outcome-linked NeedFix metrics; the source
  repository release includes the resealed accepted-task evaluation corpus
  (the corpus is not packaged in the VSIX).

## What's new in 0.11.48

- OpenCode's model list in Settings no longer crowds out newly-discovered
  models behind a heavily-toggled provider's history; today's full catalog
  fits without truncation.

## What's new in 0.11.47

- OpenCode's model list in Settings now stays current regardless of which
  panel you opened first.

## What's new in 0.11.46

- Windows: worker launches no longer fail with an unexplained authority-key
  error after upgrading.
- Windows: OpenCode now appears in model settings when it's installed.

## What's new in 0.11.45

- Windows: Claude Code can now hold the manager seat. The venv launcher's
  redirector process no longer blocks identity verification.
- Windows: native-CLI sandboxing (AppContainer confinement) works on capable
  hosts again, instead of reporting unavailable everywhere.

## What's new in 0.11.44

- Windows: creating a task no longer stalls. A child process that inherited the
  MCP server's request pipe hung before running its own first instruction, so
  every `git` the coordinator ran burned its whole timeout — task creation now
  answers in 0.09 s instead of 120 s.
- Windows: installed tools are measured again, so a card requiring
  `node>=20.0.0` and `ruff>=0.12` is no longer refused as unwinnable on a
  machine that has them.
- Windows: the authority key no longer follows a symlink, and its owner and ACL
  are checked against the open file's security descriptor.
- Windows: the reconciler heartbeat is readable again, reviewer cards no longer
  stay stuck in `processing`, and connecting a repository works again.

## What's new in 0.11.43

- The manager can now make small, hash-bound range edits directly through the
  MCP semantic-edit tools workers already use.
- A new read-only `aiworkhub_dashboard_skills` MCP surface reports measured
  skill-selection coverage — how many recorded receipts actually selected and
  injected a skill, and the streak of recent receipts that injected nothing —
  reporting "not measured" with a reason instead of a healthy-looking zero
  when the store can't be read.
- A new read-only attempt-trajectory export composes one request's audit
  history, process lifecycle, artifacts and usage into a single canonical
  JSON document, with unmeasured fields reported as `UNKNOWN` rather than
  guessed.
- Source Graph's `calls` mode now resolves a query to the one symbol that
  actually defines the queried name before returning call edges, instead of
  also matching every import, decorator or annotation that merely mentions
  it.
- The Windows sandbox report now names the exact measured cause native CLI
  execution was refused instead of one fixed blocker code. This does not
  change, and does not claim, which Windows routes are launchable.
- Oversized Source Graph analytic-mode results from the worker AI-tools MCP
  Source Graph route are now spilled to a repository-scoped store before
  being trimmed for display, so the full original stays retrievable instead
  of being discarded the moment it is truncated. The store has no eviction,
  TTL or size cap yet.
- A duplicate launch request for a task already attached to a live worker now
  returns the existing claim instead of recording a new blocked episode that
  could overwrite the original worker's ownership.

- The Models view keeps every provider visible and keeps explicitly configured
  routes reachable when a bounded catalog read comes back short. Rows are chosen
  per provider only after every provider has been seen, routes named in
  `.aiworkhub/config/models.json` are reserved under both their written and
  canonical identities, and pins that do not fit past the hard ceiling are
  counted as refused instead of disappearing.

- Model counts no longer present an upstream-truncated list as an exact total.
  Each row states the bound that produced it — declared but not discovered,
  configured with its origin unknown past the host bound, or configured past the
  source bound — and the view names the host that cut the tail instead of
  blaming the editor cap for OpenCode rows.

- Compact dashboard counters keep four significant digits, so 1000 reads as 1k
  and 1001 as 1.001k rather than collapsing to the same label, 999999999 reads
  as 1B, and the decimal separator follows your locale.

- Windows native-CLI workers now run inside the repo-scoped AppContainer
  profile and its kill-on-close Job Object when the platform, the host's
  AppContainer APIs and the launch path all confirm it, and the confinement
  report names the boundary actually in force instead of a fixed answer. The
  wiring is proved by fake-Windows behaviour tests over the real launch path;
  a live Windows canary has not run, so no live-Windows evidence is claimed.

- A Windows extension host resuming from idle no longer loses repository
  discovery to one transient handle, sharing or lock fault. The manifest read
  retries exactly once, only for an authenticated transient cause, on a
  brand-new descriptor that repeats every symlink, regular-file and identity
  check. A missing, malformed, foreign or otherwise invalid manifest still
  fails closed immediately.

- Rejected validation-only candidates now lose their one-episode replay grant,
  ensuring the next rework claim invokes a worker instead of repeating stale
  validation forever.
- Reviewer cleanup is parent-scoped, so rejecting one candidate cannot finalize
  or cancel unrelated review work.

- Review recovery now preserves fair cursor progress when its first action is
  deferred, allowing later ready reviewer chains to run in the same bounded
  reconciler pass.

- Reconciler review recovery now advances a bounded batch of durable actions
  per scan and continues past deferred chains. A busy review queue can no
  longer strand newly seeded reviewers behind one action per multi-minute pass.

- Task MCP writes now share a serialized writer boundary and reusable lock
  descriptor, reducing SQLite contention and lock churn.

- Toolchain receipt creation and validation now use one bounded executable
  fingerprint contract, eliminating false identity drift for binaries larger
  than 1 MiB during provider-free replay.

- Durable toolchain snapshots are executable-identity checked before reuse;
  stale cache facts are re-derived instead of blocking retained-candidate
  validation before its first declared command.

- Provider-free validation replay now preserves non-empty `read_first`
  requirements in its authenticated toolchain identity.
- Repeated validation-only replay can inherit the authenticated worker MCP gate
  through a mechanically failed replay without launching the provider again.
- Validation-only replay now accepts an exact authenticated retained delta
  even when the canonical parent advanced after the original attempt.
- Validation-only replay now carries the complete HMAC-bound task identity,
  allowing retained candidates to rerun gates without false receipt mismatch.
- Sealed `review_ready` candidates whose automatic reviewer child was never
  created are recovered by reconciliation without manager-launched reviewers.
- Mechanical review parks, retained-delta reroutes and pending launch failures
  preserve their authoritative lifecycle across retries.
- Read-only analysis and research no longer consume mutation-only validation or
  reviewer work.
- OpenCode workers receive isolated authentication, classic Snap launch support
  under Landlock, and explicit capacity-refusal evidence.
- VS Code LM finalization keeps valid partial finals while contradictory or
  unauthenticated terminal evidence remains rejected.

Automatic quality-review launch moves worker completion through a
system-owned correctness, security, and code-quality chain without
prematurely waking the manager. After every required lens passes, one
authenticated aggregate binds the candidate and reviewer evidence and emits
exactly one manager callback. Review automation never accepts or rejects the
implementation target; the verified manager receives only the completed
decision packet. Legacy review receipts remain compatible without blocking
later chains.

Detailed older history stays in the
[changelog](https://github.com/shrec/AIWorkHub/blob/main/vscode-extension/CHANGELOG.md).

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
  exactly 34 language/file families.
- Send focused code fragments through staged semantic edits and let the local
  bridge assemble the hash-bound final envelope without model-side full-file
  regeneration.
- Use exactly 37 currently exposed bounded Source Graph query modes for
  symbols, calls, tests, impact, complexity, ownership, hotspots, gaps and
  task-shaped context bundles.
- Preserve continuity through Session Manager, AI Memory and KB.
- Review diffs, tests, logs, artifacts, approval history and deterministic
  Quality Evidence before acceptance.
- Run a changed-file Known Bug Scanner across C/C++/CUDA, Python,
  JavaScript/TypeScript, Go, Java/Kotlin and PHP without treating heuristic
  warnings as proven failures.
- Measure whether workers used Source Graph throughout the task through
  authenticated tool-use receipts and continuous-use telemetry.
- Keep repositories isolated in separate `.aiworkhub/` authorities.
- Run on Linux (the canonical development and validation host), on WSL,
  Remote-SSH and macOS as qualified client paths, and on native Windows only
  at its measured coverage.

## Operational dashboard

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
models already authenticated in the corresponding editor or CLI. The table
lists observed runner families; Preflight reports which routes are actually
ready in this window. An optional adapter being unavailable does not block
otherwise ready models.

| Runner | Typical adapter | Requirement |
| --- | --- | --- |
| Codex | Codex CLI or VS Code Language Model | Existing Codex login or one-time VS Code consent |
| Claude | Claude Code CLI or VS Code Language Model | Existing Claude subscription login or one-time VS Code consent |
| Copilot-hosted models | VS Code Language Model | GitHub sign-in and one-time model consent |
| DeepSeek | VS Code Language Model or Copilot CLI fallback | Provider visible in VS Code; fallback uses its own stored credential |
| GLM 5.3 | VS Code Language Model or Copilot CLI fallback | Provider visible in VS Code; fallback uses its own stored credential |
| Grok | Observed Kilo/Grok worker route | Existing provider login; Preflight reports whether that route is ready |

## Source Graph and context

Source Graph is an incrementally refreshed structural repository index, not a
remote Sourcegraph service. It covers exactly 34 language/file families and
exposes exactly 37 currently exposed bounded Source Graph query modes.
Managers and workers start with low-token `focus` and `slice` queries, then
use calls, trace, impact, test mapping or typed bundles only when the task
needs them. Operations telemetry shows which modes were requested and
executed, returned evidence, workflow stage, latency, generation and
inter-call gaps.

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
