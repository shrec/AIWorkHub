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

## What's new in 0.9.87

- AIWorkHub no longer copies its whole code index before a retry. Rework is the
  thing that happens most often, and each one duplicated a 107 MB index first;
  it now indexes only the files that changed — 5 MB here — and that wait no
  longer grows as your project does.
- A cleanup job with no ending was locking the task database. Ten of them
  retried every fifteen seconds against tasks archived hours earlier, so every
  review launch failed with "database is locked" while pointing at the wrong
  database.
- Empty storage batches piled up instead of being cleaned: a Windows install had
  100 batches reporting "empty, 0 bytes" while the folder held 3.68 GB. They are
  collected now, and only when the record and the disk agree.

## What's new in 0.9.86

- Work under review could be deleted. When a task's saved work failed its
  integrity check, AIWorkHub deleted the whole workspace rather than preserving
  it — it happened here this morning and took 264 lines of finished work with it,
  silently. Failing work is now quarantined and every removal is audited.
- A task's own code could run outside its sandbox, with AIWorkHub's permissions,
  through a helper that checked whether the test tools were installed.
- A task could declare its own result: certain exit codes were trusted as "could
  not run here" when a test can produce them deliberately.
- Process identity lost a digit above a certain size, and two screens disagreed
  by one — that number is what tells a live worker from a recycled process id.
- AIWorkHub recorded nothing from Claude into its context history, and reported
  "not configured" instead of failing.
- Rejecting a task left its reviewers in the queue forever, three per rejection.
- Continuous Audit as a Service is now upheld by the system rather than by
  remembering to check, and the audit layer runs its first real pass.

## What's new in 0.9.85

- Starting a quality review took twenty to thirty minutes before anything
  happened; it now takes about half a minute. A reviewer needs its own copy of
  the project index to see the code as the task changed it, and AIWorkHub was
  duplicating the entire 107 MB index for every reviewer in order to change the
  six files the task actually touched.
- AIWorkHub now indexes only the changed files and links that small index to the
  main one. Measured here: 107 MB and ~25 minutes became 5.3 MB and 30 seconds.
- The new cost depends on how much changed, not on how large the project is — a
  project with a 1 GB index pays the same for the same six files.

## What's new in 0.9.84

- The audit trail credited the wrong model for the work: every manager action was
  recorded as if Codex performed it, including the ones Claude performed. Actions
  now name whoever actually held the manager seat, and an action that cannot be
  attributed fails rather than guessing.
- Rejecting a task left its reviewers stuck in the review queue forever, three per
  rejection, until the real work was impossible to find. Rejection now cleans up
  exactly as acceptance does.
- After a window reload, a stale manager session still reported itself verified.
  It now fails loudly instead of quietly answering the wrong question.
- A task could be started with a write scope that did not include the file it had
  to change. The worker sees only permission denied, so it retries in the wrong
  files until its budget is gone. Creating a task now warns by name.
- A database opened "read-only" was writable if its path contained a `#`, and wrote
  to a different file than the one named. Fixed everywhere and verified by sweeping
  the whole repository.

## What's new in 0.9.83

- Work that was finished and correct is no longer thrown away because of where it
  ran. Four checks depended on a home directory, a temporary path, a file
  permission or a nested launch the sandbox does not provide; any task touching
  one of them was marked failed in a way that could not be retried.
- A task that finished and was then blocked now tells you. The notification was
  being suppressed as a duplicate, and the delivery thread could be killed by a
  single unexpected error while health still showed green.
- Asking about an archived task no longer crashes the status tool, a replaced task
  no longer shows as waiting work, and one blocked task at the front of the queue
  no longer stops everything behind it.
- A task can no longer lower the bar it is judged against by changing its own
  quality settings, and a report with zero tests no longer counts as a pass.
- Cleaning up old files can no longer lose them when interrupted, reports the
  bytes it actually freed, and the recovery action the Storage panel recommends
  now exists.
- NeedFix counts fall when the work behind them finishes.

## What's new in 0.9.82

- **Source Graph could stop a search a third of the way through and still report
  it as finished, and could return the wrong lines for a symbol.** A page with
  nothing wrong ended the scan permanently, and reads used the line numbers from
  the last index rather than the file as it is now. Both fixed; every read reports
  its freshness.
- **The bug analyzers reported "clean" for languages they cannot read** - five
  C-family detectors ran against 395 Python files and returned no findings. They
  now say they do not apply.
- **A worker that committed inside its own worktree lost the work**, while the
  task still reported its required outputs as validated.
- **The manager could launch conflicting tasks in parallel**, because the default
  plan view was missing every write-conflict field.
- **A stalled runtime kept advertising itself as available**, and storage
  "Calculating" took eighty seconds on a 17 GB repository. Both bounded now.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.81

- **The runtime could report itself unavailable on a large repository.** Startup
  ran a full reconciliation scan of every task and every retained workspace before
  answering the editor, so the editor gave up and showed "MCP runtime unavailable"
  against a healthy runtime. Measured here: a 26.7 second startup, 21.6 seconds of
  it in that scan. It now runs in the background and startup answers in 0.77
  seconds.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.80

- **Live Output stopped polling forever the first time it had nothing to show.**
  Selecting a task right after launching it returned "output unavailable" and the
  poll chain was never restarted, so the panel kept showing that error while the
  worker streamed. It now restarts on every outcome, and a late task-detail reply
  no longer resurrects a panel you had already left.
- **An idempotency key could silently swallow another file's edit.** Two semantic
  edits sent with the same key but different targets counted as a repeat: the
  second was never written and still reported success, carrying the first file's
  path. A replay must now match in target as well as key.
- **Storage grew without a working bound.** Quarantined directories no batch
  claimed could never be purged, empty batches had no collector, and worker logs
  had no per-file limit. Each now bounds itself, and an oversized terminal log
  keeps the diagnostic tail the launcher reads.
- **Archived tasks stayed live and held their worktrees.** Archiving is now a
  single guarded write that verifies itself after committing, so a task cannot
  sit half-archived and pin storage forever.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.79

- **A card that changed a generated file could never be reviewed.** If a change
  touched an eval artifact or anything else Source Graph deliberately does not
  index, the reviewer never started — the launch reported success and then
  nothing happened. Prewarm now skips such a path and the reviewer still runs
  from the sealed review packet. A genuine indexing failure is still refused
  loudly.
- **The storage retention preview could not finish measuring.** It timed out
  after 90 seconds with nothing measured, so no cleanup candidate was ever
  produced. On a 29 GB repository it now completes in under nine seconds and
  reports what is actually reclaimable.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.78

- **On macOS, process identity did not exist.** A pid alone is not an identity —
  the operating system reuses them — so a reused pid could be read as a live
  worker. Darwin now supplies a real process creation time, and the eight
  launcher regressions that failed on every macOS CI run pass rather than being
  skipped. An unknown identity fails closed: a runtime directory whose owner
  cannot be identified is never reclaimed.
- **The manager callback follows whoever holds the manager seat.** Codex keeps its
  existing push path unchanged; a Claude manager is now woken through its own
  channel instead of only receiving work while it happened to be waiting.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.77

- **If you have only one model provider installed, finished work could never be
  accepted.** The acceptance gate demanded a review from a different vendor than
  the one that did the work, so on a single-provider setup nothing could pass,
  however complete and green it was.
- Reviewer independence is now a recorded ladder — `cross_provider` degrading to
  `cross_model_same_provider` and then to `same_model_fresh_context` — and the
  level actually achieved is written into the acceptance evidence, so you can see
  exactly how independent each review was.
- A reviewer that cannot read the review packet is still refused for its lens,
  and a review that cannot be attributed to a worker provider is still refused.
  Every other safeguard is unchanged.
- Source Graph no longer reports success for a build that indexed nothing, and a
  duplicate declaration in one PHP file no longer aborts the whole index.
- Restoring a quarantined worktree reinstates its git registration and says so
  when it cannot, rather than leaving the directory attributed to nobody.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.74

- A quality lens can no longer be satisfied by a reviewer that could not inspect
  anything. A reviewer whose sandbox gives it no file-read tool cannot open the
  review packet it is handed as a file path — and its empty report still came
  back shaped identically to a real review, so a required lens counted as
  satisfied by an inspection that never happened. Observed four times.
- The gate now marks such a lens `reviewer_could_not_inspect`, but only on a
  positive signal: findings that are all `process_limit`, or usage telemetry
  that is present and records zero activity.
- Missing telemetry deliberately stays "unknown" and keeps satisfying the lens.
  Most honest reviews carry no inspection telemetry, and demanding proof of
  inspection rejects real work — absence of evidence is not evidence of absence.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.73

- A read-only SQLite open is now actually read-only. Every such connection was
  built by interpolating the path into a URI string, and in SQLite URI syntax a
  `#` in the path swallows the `?mode=ro` query — so the database opened
  read-write with create-if-missing, at a path truncated at the `#`. Verified
  directly: the old form accepted a write and left a stray file on disk, while
  the corrected form refuses the write and creates nothing.
- All eight affected call sites now route through one shared helper rather than
  eight independent strings that each had to stay correct forever, and the
  helper applies two independent guarantees: percent-encoding so the query
  survives URI parsing, and `PRAGMA query_only = ON` so a write is refused even
  if it did not.
- Every converted call site keeps its exact previous timeout, so behaviour
  under concurrent load is unchanged.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.72

- Storage retention actually reclaims. Worktree eligibility reported zero
  candidates while the repository sat at 43.3 GB against a 5 GB cap, because
  every attempt workspace stayed pinned and nothing released a superseded one.
  A superseded attempt whose successor has been sealed is now eligible, and
  exceeding the cap forces reclamation of the oldest superseded lineage.
- Live-worktree protection is keyed on the field the claim path actually writes,
  so a `processing` or `review` card's worktree is protected while it runs. It
  was keyed on a field production only writes after acceptance, leaving every
  in-flight worktree unprotected.
- Protection no longer depends on how large the task table has grown, and the
  planner fails closed when task lineage cannot be read.
- Measured here after the fix: 140 reclaimable worktrees, 17.4 GB, with the two
  live workers correctly protected.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.71

- A card owner can now release its own claim. The card-scoped write-action set
  omitted `launch-failed`, so a reconciled reservation could leave its card
  stranded in `processing` forever with no live provider able to finish it.
- Both authority call sites now share one named action set, so they cannot
  drift apart. Codex authority is unchanged.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.70

- Coordinator routing resolves from the active verified manager route, so a
  Claude-routed repository no longer sits permanently at `route_pending`
  waiting for a Codex thread that will never appear.
- The dashboard no longer raises a Manager coordination Route warning that no
  operator action can clear, and manager identity and coordinator routing can
  no longer disagree inside the same response.
- Codex routing behaviour is unchanged, and a repository with no verified
  route fails closed rather than defaulting to a provider.

See the packaged **Changelog** for the complete release summary.

## What's new in 0.9.69

- A verified Claude manager now receives worker callbacks in its active session
  instead of waiting in the inbox until it happens to poll, so `review_ready`
  and `worker_failed` transitions reach the manager as they happen.
- The lease and ack contract is unchanged: one verified manager route holds a
  batch, ack stays mandatory, an unacked batch stays redeliverable, and a route
  whose provider, repository or session identity does not match never receives
  it.
- `dispatcher_health` reports a truthful provider-specific state on a Claude
  route instead of flagging an unregistered dispatcher where none is expected.
- Windows and macOS CI jobs run explicit platform-owned regression manifests and
  fail if a manifest entry collects nothing, so CI can no longer pass silently
  on zero collected platform tests.

See the packaged **Changelog** for the complete release summary.

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
