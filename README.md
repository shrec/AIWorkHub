# AIWorkHub

AIWorkHub (AWH) is a repository-bound development brain/control plane for
AI coding agents. Opened against any Git checkout, it binds task lifecycle,
Source Graph code discovery, Session Manager continuity, bounded AI Memory,
KB lookups, isolated worker launch, and Codex/Claude callback routing to
that exact repository. Every repository owns its own durable state under a
repository-local `.aiworkhub/` directory (task queue, sessions, memory, KB,
Source Graph, routing config) -- there is no shared server, no central
database, and no host-specific path baked into the tool.

The primary interface is the **AIWorkHub VS Code extension**: a native
editor-tab Webview that talks to a repository-scoped Task MCP stdio child
process. It never opens a browser, binds a port, or exposes a LAN address.
The same `aiworkhub` Python package is also usable headless as an MCP
server for any MCP-capable client (Claude Code, Codex, other agent hosts).

- Source of truth: this repository's own `.aiworkhub/tasking/task_queue.sqlite`
  (created/repaired by **Init Repo**, never a path outside the checkout)
- MCP transport: stdio (no HTTP listener anywhere in the runtime)
- Default mode: read-only and launch-disabled until explicitly enabled
- Multi-repository isolation: one MCP stdio child and one `.aiworkhub/`
  state directory per opened repository; nothing is shared across repos
- Local model adapters: Claude Code CLI, Codex CLI, and `deepseek_copilot_cli`
  (official GitHub Copilot CLI driven in BYOK mode against DeepSeek's
  OpenAI-compatible API)
- DeepSeek adapter: `deepseek_copilot_cli` is the local-launch adapter for every
  `deepseek_*` runner; `deepseek_manual` remains an explicit non-launchable
  fallback only

## Five-Minute Quickstart (VS Code)

1. **Install the extension.** Build or download the VSIX (see
   [Publishing](docs/PUBLISHING.md)) and run
   `code --install-extension vscode-extension/dist/aiworkhub-<version>.vsix`.
   Works identically over Remote-SSH -- the extension kind is `workspace`, so
   the MCP child and Python runtime run on the workspace host.
2. **Open a repository** in VS Code, then run `AIWorkHub: Open Dashboard`
   (or `AIWorkHub: Select Repository` first in a multi-root window).
3. **Init Repo.** On first open the dashboard shows an explicit
   **Initialize AIWorkHub** action. Click it once -- this creates
   `.aiworkhub/project.json`, the storage registry, a fresh canonical
   `.aiworkhub/tasking/task_queue.sqlite`, and the Source Graph store. The
   initial Source Graph index starts asynchronously; one repo-bound daemon
   then keeps it fresh with non-overlapping incremental refreshes.
   Nothing is created before this explicit step, and it is safe to run
   again (idempotent).
4. **Work the queue.** The dashboard tab shows pending/processing/review
   tasks, per-topic/runner summaries, cost/usage, and callback-bridge
   health, all read live from `.aiworkhub/`. Selecting a task shows its
   detail and, for a running worker, its live stdout/stderr (**Live
   Output**).
5. **Workers close the loop through Codex callback routing** (see
   [Callback Bridge](#callback-bridge-task-mcp---originating-codex-thread)
   below): a claimed task's terminal state (review-ready, blocked, failed,
   timed out) wakes the exact Codex thread that registered it -- no manual
   polling or copy/paste. Claude callback delivery reuses the same durable
   outbox/lease/retry machinery and reaches the exact originating Claude
   session, via a cooperative MCP callback inbox or the `claude --resume` CLI
   transport; see [Claude callback capability](#claude-callback-capability)
   below (and
   [Getting Started](docs/GETTING_STARTED.md#6-codex-callback-routing--claude-callback-capability))
   for the current Claude transport modes and limitations.

See [Getting Started](docs/GETTING_STARTED.md) for the full walkthrough
(including headless/CLI-only setup) and [Architecture](docs/ARCHITECTURE.md)
for how the pieces fit together.

## 0.6.31 coordinator lifecycle and callback reliability

- Claude callback **channel** (`aiworkhub-callback-channel`): a native Claude
  Code channel that pushes terminal review callbacks into a live session with
  no polling -- the Claude-side analog of the Codex sideband push. Research
  preview; live delivery must still be confirmed against a real
  `claude --channels` session.
- The callback channel push loop backs off when idle instead of spinning a CPU
  core when the session is not a verified manager or no callback is due.
- Coordinator task lifecycle: a repository-local, owner-only coordinator token
  is the capability for the repository owner (no separately exported env token
  required); new `archive`/`supersede` commands; and `reject-review` takes an
  explicit disposition (pending | blocked | archived | superseded). All are
  coordinator-capability gated and atomic.
- A readonly task may declare `allowed_writes: []` on purpose; only a missing
  key is rejected. The validator reports a cached/zero-hit Source Graph as
  stale rather than contradicting a successful call. Terminal substatuses
  (dependency-blocked, liveness-lost, required-output-unchanged) map to the
  correct callback transition instead of silently becoming review-ready.
- A dependency's declared outputs are materialized into a dependent's isolated
  worktree so a promoted-but-uncommitted artifact is visible to it. The
  callback dispatcher fails closed on an unresolved repository identity and
  recovers through a watchdog.

## 0.6.30 reliability and quality foundation

- Callback delivery is authorized by an expiring per-window, per-repository
  route lease. A window renews and removes only its own record; ambiguous,
  expired, or foreign-repository routes fail closed.
- Windows MCP launch preflights repository virtual environments and safely
  falls back through `py -3` and `python`, with `shell=false` and support for
  paths containing spaces.
- Source Graph, Session Manager, AI Memory, and KB are canonical repo-local
  authorities and are available to both managers and workers.
- The Quality Evidence Engine detects declared project tooling without
  installing dependencies and normalizes test, SARIF, coverage, benchmark,
  and read-only AI-review evidence. `not_available` is never treated as
  `passed`.

## Install For Local Development (headless / no VS Code)

From this directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

If the parent environment already has `mcp` installed, direct execution also works:

```bash
PYTHONPATH=src AIWORKHUB_REPO=/path/to/checkout python3 -m aiworkhub.server
```

`AIWORKHUB_REPO` selects the target repository; it works the same way on
Linux, WSL, and native Windows Python -- it is resolved with `Path.resolve()`
against whatever checkout path the host platform provides, never a
hardcoded host-specific path.

## MCP Client Config

Example MCP server entry:

```json
{
  "mcpServers": {
    "aiworkhub": {
      "command": "python3",
      "args": [
        "-m",
        "aiworkhub.server"
      ],
      "env": {
        "PYTHONPATH": "/path/to/checkout/src",
        "AIWORKHUB_REPO": "/path/to/checkout",
        "AIWORKHUB_ALLOW_WRITES": "0"
      }
    }
  }
}
```

Set `AIWORKHUB_ALLOW_WRITES=1` only for trusted local automation. A real
model launch additionally requires `AIWORKHUB_ALLOW_LAUNCH=1`; both gates
default to `0`. Launches are local, shell-free, exact-task-bound, process-group
tracked, timeout-bounded, and collision-checked.

## Exposed Tools

Read-only by default:

- `aiworkhub_task_health`
- `aiworkhub_task_review_queue`
- `aiworkhub_task_list`
- `aiworkhub_task_show`
- `aiworkhub_task_pending_for_runner`
- `aiworkhub_task_collision_guard`
- `aiworkhub_task_usage_report`
- `aiworkhub_task_audit_log_read`
- `aiworkhub_task_completion_inbox`
- `aiworkhub_task_stale_recovery_recommend`
- `aiworkhub_task_cost_ledger`
- `aiworkhub_agent_task_status`
- `aiworkhub_agent_collect_result`
- `aiworkhub_agent_list_processes`
- `aiworkhub_cli_adapter_plan_readonly`
- `aiworkhub_cli_adapter_audit_summary_readonly`
- `aiworkhub_cli_adapter_report_readonly`

Write-gated:

- `aiworkhub_task_auto_pickup`
- `aiworkhub_task_mark_review`
- `aiworkhub_task_reject_review` (Codex requeues a failed review with feedback)
- `aiworkhub_task_mark_done`
- `aiworkhub_task_export_jsonl`
- `aiworkhub_task_queue_request`
- `aiworkhub_agent_launch_task` (requires both write and launch gates)
- `aiworkhub_agent_cancel_task` (requires both gates)

`aiworkhub_task_queue_request` is the legacy intent/audit API and still launches
nothing. `aiworkhub_agent_launch_task` is the real runtime API. It validates the
exact pending task, runner/topic, allowed-write boundary, collision state,
adapter, and concurrency cap before starting a process. The worker's first
operation is exact `taskctl claim-start <task_id>`, and success is not reported
as complete until the task reaches `review`.

## Task Dispatch, Validation & Acceptance

**Dispatch.** Tasks are claimed, never pushed: `aiworkhub_task_auto_pickup`
matches an exact `runner`/`topic` pair against the pending queue in
`.aiworkhub/tasking/task_queue.sqlite` and hands the worker one task card
carrying its `allowed_writes`, `forbidden` actions, and `required_outputs`. A
worker never picks a task from another topic. `aiworkhub_agent_launch_task`
independently re-validates the exact pending task, runner/topic, adapter,
allowed-write boundary, and collision state before starting a process.
A task's `depends_on` edges gate its readiness: it is not eligible for
dispatch while a declared dependency is outstanding or while its
`allowed_writes` overlaps an in-flight dependency's own write set. The
resolved dependency/readiness state is captured in an immutable plan
snapshot at dispatch time, so a worker's card reflects the exact Plan-DAG
state it was cleared against.

Every task then passes through three independent verification layers before
it can be accepted:

1. **Worker self-validation.** Inside its own isolated worktree, the worker
   runs the exact validation commands its task card lists (tests,
   `python3 -m json.tool`, etc.) before calling
   `aiworkhub_task_mark_review`/`taskctl review`. This is the worker's own
   claim -- not yet trusted by the coordinator.
2. **Independent coordinator review.** The coordinator does not take the
   worker's claim on faith: it independently reruns the same validation
   commands against the worker's actual changed files, confirms the diff
   stays inside `allowed_writes`, and confirms nothing in `forbidden` was
   touched -- before promoting any file out of the worker's isolated
   workspace.
3. **Audit-ledger acceptance gate.** Every required tool call a worker makes
   (Source Graph, Session Manager, AI Memory, KB) is recorded to an
   HMAC-authenticated MCP audit ledger. A worker's textual claim that it used
   a required tool is not sufficient by itself -- the coordinator checks the
   ledger for the actual call before accepting a code task.

A task only reaches accepted/`done` after all three layers pass: worker
validation -> independent coordinator re-validation -> audit-ledger receipt
check. Workers stop at `review`; only the coordinator finalizes `done`.

## DeepSeek (`deepseek_copilot_cli`) Adapter

`deepseek_copilot_cli` launches the installed official GitHub Copilot CLI in
"bring your own key" (BYOK) mode against DeepSeek's OpenAI-compatible API
(`https://api.deepseek.com/v1`). Every `deepseek_*` runner routes to this
adapter for local launch; `deepseek_manual` stays an explicit fallback only.
A DeepSeek-labeled task is never routed to a GitHub-hosted Claude/GPT model.

Supported models: `deepseek-v4-pro` (production coding default) and
`deepseek-v4-flash`.

### One-time secure credential bootstrap

The DeepSeek API key is stored in a mode-0600 file **outside the repository**
(default `~/.config/aiworkhub/deepseek_copilot_credential.json`, override
with `AIWORKHUB_DEEPSEEK_CREDENTIAL`). The key is read via `getpass` (never
on a command line) and is loaded only on the coordinator side at launch time. It
enters **only** the launched child process as `COPILOT_PROVIDER_API_KEY` — never
argv, task cards, logs, audit events, dashboard payloads, or Git.

```bash
aiworkhub-deepseek-credential set        # prompts for the key (hidden input)
aiworkhub-deepseek-credential status     # secret-free readiness JSON
```

The loader rejects symlinks, group/world-accessible files, wrong ownership,
empty keys, a path inside the repository, and any non-DeepSeek endpoint. A
missing/invalid credential makes launch fail **before** `taskctl claim-start`,
so the task is left pending/unclaimed (never claimed on a missing credential).

The non-secret provider environment passed to the child is the OpenAI-compatible
provider type, the `https://api.deepseek.com/v1` base URL, the selected model,
and the API key; the launcher declares the key as secret-redacted via the CLI's
`--secret-env-vars` flag. Copilot runs non-interactively (`-p` single prompt
token, `--output-format json`, `--allow-all-tools --no-ask-user`,
`--no-remote --no-remote-export`, `--disable-builtin-mcps`, explicit `-C`/
`--model`) with permissions that stay subordinate to the outer Landlock/
bubblewrap filesystem sandbox (no `--allow-all-paths`/`--yolo`).

### Adapter readiness

Read-only adapter readiness is surfaced in the `adapter_readiness` field of the
`aiworkhub_completion_inbox` MCP tool output and in the dashboard's
`adapter_readiness` snapshot section (folded into existing surfaces so the
frozen v1 tool contract stays at 33 tools). Per adapter it reports `installed`,
`credential_present`, `endpoint`, `supported_models`, `launchable`, and the
exact non-secret `blocker_reason`. Credential contents and hashes are never
exposed. `aiworkhub-deepseek-credential status` prints the same secret-free
readiness from the CLI.

## Callback Bridge (Task MCP -> originating Codex thread)

The callback bridge wakes the exact Codex thread that registered a task the
moment it reaches a terminal state (`review_ready`, `blocked`,
`launch_failed`, `validation_failed`, `scope_rejected`, `timed_out`,
`cancelled`) -- so the coordinator no longer has to poll or be manually
copy/pasted a result. `pending`, `processing`, Codex `done`, and Codex
`reject-review` never enqueue a wake.

Every task that has been claimed reaches the same canonical terminal surface:
`status=review`, `worker_status=review_ready`, plus a normalized
`review_outcome`/bounded `review_reason`. This includes blocked/promotion
conflicts and a missing/stale worker (`process_lost`, delivered through the
blocked callback outcome bucket). Automated recovery never moves a claimed
task back to `pending`; only an explicit coordinator `reject-review` does so.

**Origin thread capture.** `taskctl.py add-card` auto-captures a well-formed
`CODEX_THREAD_ID` env var into the new task's immutable `origin_thread_id`
when the card does not already declare one. A malformed value is silently
skipped (registration never fails on it). Legacy tasks without a thread stay
valid and callback-disabled until a coordinator repairs them:

```bash
CODEX_THREAD_ID=<uuid> python3 AITools/taskctl.py bind-thread <task_id> --runner codex
```

A rebind to a *different* thread than the one already bound is rejected
(immutable once set).

**Durable outbox.** Terminal transitions are enqueued into
`AITools/taskdb.py`'s SQLite `callback_outbox` table, deduplicated by
`(task_id, transition, origin_thread_id, episode_id)` -- an idempotent
reimport, retry, or duplicate transition can never wake the thread twice,
while a Codex-rejected and newly claimed episode may produce its own wake.
Entries move through
`pending -> inflight -> delivered` or `dead_letter`, with a lease + bounded
retry/backoff and restart recovery (an expired lease is reclaimed by the next
poll).

**Busy/active-thread deferral parks durably, never dead-letters (B416).** A
temporary active/busy Codex thread (`BusyThreadError` and its subclasses --
`ActiveThreadSteerDeferralError`, `SidebandThreadBusyError`) is a
fundamentally different condition from a genuine transport/protocol failure,
and the two no longer share a failure budget. `AITools/taskdb.py::
defer_batch_busy` reschedules the whole batch via `callback_batches.
not_before_at` (capped exponential backoff) and **never dead-letters it, no
matter how many times it is claimed** -- a thread that is still busy after
five, ten, or a hundred claims stays parked, exactly like the real
production case that motivated this fix (a thread reporting 45 in-progress
turns was incorrectly dead-lettered after five retries). A genuine
transport/protocol failure (any other `AppServerError` -- a crashed/
unreachable App Server, `SidebandUnavailableError`, `SidebandRejectedError`,
malformed protocol) goes
through `AITools/taskdb.py::fail_batch_transient` instead, which retains the
original bounded retry/dead-letter route on its own independent
`hard_failure_count`, untouched by any busy deferral.

The reschedule is **non-blocking**: `CallbackBridge._process_batch` never
sleeps. `claim_pending_callback_batch` simply skips a `pending` batch whose
`not_before_at` is still in the future and moves on to the next candidate,
so one thread's busy backoff window never blocks progress on any other
thread's due work -- and a thread that already owns a not-yet-due batch is
excluded from new-batch formation (its later-arriving terminal events wait,
unassigned, for that SAME batch, never a competing second one).
Redacted status distinguishes the two conditions without exposing
origin_thread_id or callback payload text: `waiting_for_thread_idle_batch_count`
(parked, busy) vs `pending_genuine_failure_batch_count` (failing).

**Batched delivery (B402).** A burst of near-simultaneous terminal events on
the SAME origin thread (the measured failure: eight review_ready events at
once produced one inflight plus seven pending single-item callbacks) is
coalesced into **one leased delivery batch per thread**, delivered as a
**single Codex turn** -- never one turn per task. `AITools/taskdb.py`'s
`callback_batches` table holds the batch's durable identity/lease/attempts;
every member outbox row carries that `batch_id` and transitions with it
(`mark_batch_delivered`/`defer_batch_busy`/`fail_batch_transient`/
`mark_batch_dead_letter` act on the whole batch together, never partially).
A batch never spans threads, and a
thread never has two batches in flight at once. Membership is fixed at
formation time and never re-scanned on restart/lease-reclaim -- only
re-pruned for staleness (`_task_still_in_matching_terminal_state`, run
before every (re-)lease and before any App Server process starts) -- so an
event that arrives for the same thread *while* a batch's turn is running
waits, uncoalesced, for the very next batch immediately after that turn
completes, rather than forcing a second parallel turn. The fixed batch
prompt lists each member's validated `task_id`/transition/event id plus the
bounded total count, and explicitly instructs Codex to inspect the complete
trusted review queue -- so a task that changed state mid-turn is still
covered without a redundant wake.

**Configurable App Server timeout / lease.** No 60-second implicit timeout
exists anywhere in the bridge. `--app-server-timeout-seconds` /
`--lease-seconds` / `--max-batch-members` (or the
`AIWORKHUB_CALLBACK_APP_SERVER_TIMEOUT_SECONDS` /
`AIWORKHUB_CALLBACK_LEASE_SECONDS` / `AIWORKHUB_CALLBACK_MAX_BATCH_MEMBERS` env
vars) override the safe defaults (1800s timeout / 2100s lease, sufficient
for a long CEO review turn). The lease must always exceed the timeout by a
real margin (default 300s) -- an invalid combination is rejected at startup,
never silently run.

**Dead-letter recovery.** A coordinator-only, audited CLI requeues one
dead-lettered outbox row only if its task is STILL in the matching eligible
terminal state/episode right now (otherwise it supersedes the row -- a
stale wake for a task that already moved on is never silently retried):

```bash
python3 AITools/taskctl.py callback-recover-dead-letter <outbox_id> --runner codex
```

**Transport.** The bridge speaks the real local Codex App Server protocol --
a single `codex app-server --listen stdio://` subprocess, newline-delimited
JSON-RPC: `initialize` -> (wait for success) -> `initialized` -> `thread/resume`
(the bound thread) -> `turn/start` (fixed prompt, deterministic
`clientUserMessageId`) -> wait for the matching `turn/completed`. The
rejected/nonexistent transport (`codex thread status`,
`codex exec --thread-id/--client-id/--no-remote`) is never called or
emulated. If the origin thread is busy, the WHOLE batch is deferred (returned
to `pending`, non-blockingly rescheduled via `not_before_at`) -- the bridge
never starts a parallel CEO turn and never dead-letters a busy deferral (see
B416 above).

**Fixed prompt only.** The turn text is a coordinator template containing
only the validated `task_id`(s), normalized terminal transition(s), and
event id(s), plus the bounded member count for a batch. Worker output, logs,
objectives, error text, artifacts, tool input, and the full `origin_thread_id`
are never interpolated into it; the prompt instructs Codex to inspect the
task(s) via trusted MCP/taskctl tools instead.

**CLI.**

```bash
aiworkhub-callback-bridge run-once      # process at most one pending BATCH, then exit
aiworkhub-callback-bridge daemon        # poll continuously (idle polling starts zero
                                          #   Codex/model processes and uses zero tokens)
aiworkhub-callback-bridge status        # redacted health: bound/unbound, per-state
                                          #   outbox+batch counts, batch size, oldest
                                          #   pending age, last delivery -- never a full
                                          #   thread id
aiworkhub-callback-bridge dry-run TASK_ID STATE
                                          # disposable canary: builds the prompt/argv,
                                          # never starts a real App Server subprocess

# All three accept --app-server-timeout-seconds/--lease-seconds/--max-batch-members
```

**systemd (user) example for automatic restart** --
`~/.config/systemd/user/aiworkhub-callback-bridge.service`:

```ini
[Unit]
Description=AIWorkHub MCP callback bridge (Codex App Server outbox consumer)

[Service]
Type=simple
Environment=AIWORKHUB_REPO=/path/to/checkout
WorkingDirectory=/path/to/checkout
ExecStart=/path/to/checkout/.venv/bin/aiworkhub-callback-bridge daemon
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now aiworkhub-callback-bridge.service
```

Uses the existing local Codex authentication (the `codex` CLI's own login);
the bridge stores no API/access token of its own.

### Claude callback capability

A Claude coordinator is a first-class manager provider, keyed by its
persistent Claude Code `session_id` (Codex is keyed by `thread_id`). The same
durable `callback_outbox`/`callback_batches` machinery -- lease/retry/backoff,
busy-deferral, and batching -- applies unchanged; the `provider` column keeps
the Codex and Claude lanes isolated so both managers run in parallel without a
repository-global toggle.

Claude terminal callbacks reach the **exact originating Claude session** by one
of three modes, never a fabricated delivery:

- **`channel` push -- Codex parity: no polling, the callback wakes the model
  itself.** A Claude Code *channel* is a plain MCP server that Claude Code
  spawns and that pushes events into the running session; the model wakes and
  acts with no polling and no token burn while idle.
  `aiworkhub-callback-channel` (`callback_channel.py`) is that channel: a
  background thread claims the next review callback for this session through the
  same durable outbox (`claude_callback_wait`/`_ack`, never a second database)
  and emits it as a `notifications/claude/channel` event whose text names only
  the task_id(s)/transition(s) and directs the model to inspect the review
  queue -- never worker output. Register it in `.mcp.json` and launch Claude
  Code with the channel enabled:

  ```json
  { "mcpServers": { "aiworkhub": { "command": "aiworkhub-callback-channel" } } }
  ```
  ```bash
  claude --dangerously-load-development-channels server:aiworkhub
  ```

  Channels are a Claude Code research-preview feature (require claude.ai / API
  auth; not on Bedrock/Vertex/Foundry; the session must be launched with the
  channel enabled). The exact flag surface is preview-stage and unverified
  against a live binary here -- the wire shape is unit-tested, live delivery is
  confirmed against a real `claude --channels` session.
- **`manager_inbox` -- cooperative pull.** The verified manager owns a two-phase
  MCP long-poll inbox -- `aiworkhub_claude_callback_wait` returns the next batch
  belonging to this exact session and `aiworkhub_claude_callback_ack` durably
  acknowledges it. Reaches the exact chat while the manager is actively waiting
  on its inbox; no second process is spawned.
- **`cli_resume`.** A repository-bound `claude --resume <session_id> --print`
  transport (`ClaudeCliResumeClient`) delivers to a resumable local Claude Code
  CLI session, gated on an `event_id`/`request_id` acknowledgement echo before a
  delivery counts as `delivered`.

**Why not the Codex app-server trick.** Codex's sideband transport wedges
`aiworkhub/app_server_mux.py` between the VS Code Codex extension and its owned
`codex app-server` child to inject a live turn into the visible thread. Claude
Code exposes **no** equivalent injectable app-server (confirmed against the
official docs and the open `anthropics/claude-code` external-message-injection
feature requests), so that exact topological trick has no Claude analog -- the
`channel` push above is the supported Claude-native equivalent instead
(`ClaudeCallbackAdapter`'s `panel`/`wake_transport` seam, B855, remains
available for any future in-editor wake endpoint). See
[Getting Started](docs/GETTING_STARTED.md#6-codex-callback-routing--claude-callback-capability)
for the current capability summary.

## App Server Mux + Sideband Transport

The sideband bridge must wait until the VS Code extension has been reloaded
through the configured mux.  Its lease must also cover the 45-second App
Server deadline plus the 300-second recovery margin (minimum 345 seconds).

A separately spawned bridge App Server can only ever see the VS Code OpenAI
extension's owned thread from the outside: the extension spawns and owns its
own `codex app-server` child over a private stdio pipe, and `thread/resume`/
`turn/*` are scoped to the App Server INSTANCE that owns the turn, not the
thread id in the abstract. `aiworkhub/app_server_mux.py` closes this
topologically: installed as the extension's `chatgpt.cliExecutable`, it
transparently `execvp`s the real Codex binary for every non-`app-server`
invocation (exact argv/exit behavior), and for `app-server` invocations
becomes the one process proxying the extension's stdio to a real child App
Server transparently in both directions while exposing one authenticated
local Unix sideband socket per mux instance (owner-only directory/socket/
capability file, `SO_PEERCRED` check, three-method allowlist, bounded
sizes/deadlines, no token/thread/prompt logging, readiness gated on
passively observing the extension's own handshake, no separate-App-Server
fallback ever).

**B472 -- multiple concurrent VS Code windows.** Each mux instance binds a
randomly-named `<instance_id>.sock`/`<instance_id>.cap` pair under
`sideband_dir` (never a fixed shared name, and never unlinking a pre-existing
path -- a collision regenerates a fresh id) and publishes a same-uid,
owner-only registry file under `sideband_dir/instances/` recording routing
identity only: instance id, pid, pid start-time (guards stale-PID-reuse
liveness), the socket/capability paths, and the set of thread ids this
instance's OWN extension traffic has bound (`thread/resume`/`turn/start`/
`turn/steer` requests observed on the extension->child pump -- a
sideband-issued probe never counts). `callback_bridge.py::SidebandCallbackClient`
resolves the single live instance owning a given `origin_thread_id`
(`find_owning_sideband_instances`) before every call and addresses only that
instance; missing, stale, or ambiguous ownership durably parks the whole
batch (`SidebandOwnerNotFoundError`/`SidebandOwnerAmbiguousError`, both
`BusyThreadError` subclasses) instead of guessing or fanning out. This
closes the exact B471 live-canary blocker: three concurrent mux PIDs sharing
one fixed `sideband.sock`/`sideband.cap` path, where the newest process
silently shadowed the origin thread owner's endpoint.
`CallbackBridge(transport="sideband")` reuses the same B407
`select_steer_target` routing and the existing outbox/lease/retry machinery.

`scripts/install_vscode_app_server_mux.py` is dry-run/check/print-config
only -- it never touches VS Code settings, the installed extension, systemd,
or the live callback DB:

```bash
python3 scripts/install_vscode_app_server_mux.py --check
python3 scripts/install_vscode_app_server_mux.py
```

Codex owns applying the printed `chatgpt.cliExecutable` value, reloading the
extension host, the live canary against the extension-owned thread, and
finally switching the callback bridge to `transport="sideband"` and
re-enabling its service.

## VS Code Dashboard

Use the repository-local **AIWorkHub** VS Code extension. Its native Webview
reads the canonical dashboard snapshot through a repository-local Task MCP
stdio session. It does not open a browser, expose a LAN address, start an HTTP
listener, or require port forwarding. Installation and operation are documented
in `tools/vscode-aiworkhub-task-operations/README.md`. `aiworkhub/dashboard.py`
contains only the read-only ``build_snapshot``/``build_task_detail`` builders
the Webview's bounded MCP tools call -- there is no in-package HTTP server,
browser launch, or fixed listen port.

The extension packages as a self-contained VSIX
(`vscode-extension/dist/aiworkhub-<version>.vsix`, built by
`node vscode-extension/test/package-vsix.js`) that bundles the canonical
`aiworkhub` Python runtime and Webview assets under an extension-local
`runtime/` directory, so installing it requires no repository checkout,
editable install, or network-time package install, and it opens as a normal
editor tab under Remote-SSH.

The dashboard editor tab has no manual model-probe or canary surface: there
is no "Model capabilities" panel, no `vscode.lm.selectChatModels` discovery
action, and no credit-consuming GLM canary prompt anywhere in the extension.
Model routing and task execution run only through the real autonomous
worker adapters (`deepseek_copilot_cli` etc., see `deepseek_credentials.py`
above), never through a manually-triggered diagnostics probe.

Task-bound AI context telemetry is metadata-only. Source Graph, Session
current-state, AI Memory, and KB sections report requested/executed state, hit
counts, bytes, hashes, truncation, and degraded reasons. The dashboard shows
context injected versus worker-acknowledged separately; injected context is not
treated as consumed unless the worker emits the bounded
`aiworkhub.task_mcp.worker_context_receipt.v1` receipt. Raw context bundle content
is not stored in process events or dashboard payloads.

Context policy is task-type aware. Code tasks require non-empty Source Graph
evidence unless a card explicitly marks Source Graph non-gating. Data
classification tasks may use an immutable input shard as the primary context,
with empty Source Graph reported honestly as zero hits rather than as token
savings. Raw-context-versus-bundle byte reporting is a deterministic estimate,
not a token or cost claim.

## Runtime Gates

```bash
export AIWORKHUB_ALLOW_WRITES=1
export AIWORKHUB_ALLOW_LAUNCH=1
export AIWORKHUB_MAX_PROCESSES=4
```

Keep both gates disabled for worker-facing MCP configurations. Enable them only
in the coordinator's local MCP process. A launched worker does not inherit the
launch gate.

## Write-Gate Audit Log

Every blocked write attempt is recorded to an append-only JSONL audit log.

**Default path:** `.aiworkhub/runtime/process_logs/audit.jsonl` (relative to
`AIWORKHUB_REPO` root).

**Override path:** set `AIWORKHUB_AUDIT_LOG_PATH` to an absolute path.

**Format:** one JSON object per line:

```json
{"timestamp":"2026-07-04T12:00:00+00:00","tool_name":"auto-pickup","action":"blocked_write","blocked_reason":"...","caller_info":{"pid":12345,"env_vars_checked":{"AIWORKHUB_ALLOW_WRITES":"<set>","AIWORKHUB_AUDIT_LOG_PATH":"<unset>"}}}
```

**Safety guarantees:**
- Never logs secret values, tokens, passwords, or environment variable VALUES — only NAMES and `<set>`/`<unset>` status.
- Append-only — existing entries are never overwritten.
- Log write failures print a warning to stderr but never crash the server.
- `AIWORKHUB_ALLOW_WRITES` default remains `0` (off) — the audit log does not enable writes.

## Smoke Test

```bash
bash tests/smoke.sh
```

Broader local verification:

```bash
python3 -m pytest -q tests
bash tests/test_mcp_stdio_subprocess_client_smoke_b109_v1.sh
bash tests/test_mcp_queue_request_tool_b286_v1.sh
bash tests/test_mcp_stale_recovery_orchestrator_b287_v1.sh
bash tests/test_mcp_cost_ledger_aggregator_b288_v1.sh
python3 -m pytest -q tests/test_runtime_adapters.py tests/test_process_launcher.py tests/test_dashboard.py
python3 -m pytest -q tests/test_deepseek_copilot_adapter_b343_v1.py
python3 -m pytest -q tests/test_callback_bridge.py
python3 -m pytest -q ../../AITools/test_taskctl_origin_thread_callback_b366_v1.py ../../AITools/test_taskctl_origin_thread_callback_b384_v1.py
```

## Write-Gate Audit Smoke Test

```bash
bash tests/test_write_gate_audit_v1.sh
```

## Roadmap: Concepts Under Consideration (Kimi-Atlas-Inspired)

This section tracks a set of Kimi-Atlas-inspired design ideas against the
current codebase. Not all of them are done -- each item below is marked
explicitly as implemented or still roadmap-only; absence of a mark elsewhere
in this document is not a claim of completion.

**Implemented:**

- **Deterministic lifecycle FSM** -- today's status transitions
  (`pending` -> `processing` -> `review` -> `done`/`blocked`) run through an
  explicit, exhaustively enumerated finite-state machine so every transition
  is provably reachable and every non-transition is provably rejected.
- **Plan-DAG dependencies/readiness** -- `depends_on` edges gate task
  readiness, with write-overlap blocker detection against in-flight
  dependencies and an immutable plan snapshot captured at dispatch time (see
  [Task Dispatch, Validation & Acceptance](#task-dispatch-validation--acceptance)
  above).
- **Deterministic verification lenses** -- named, composable verification
  passes produce independent pass/fail verdicts as part of the coordinator's
  independent re-validation, instead of one monolithic validation script.
- **Independent coordinator accept** -- the coordinator re-validates and
  hash-gates a worker's changed files against its own rerun, independent of
  the worker's self-reported validation, before promoting any file out of
  the worker's isolated workspace.

**Still roadmap -- design ideas only, not implemented:**

- **Combined-tree differential gate** -- diff the worker's full changed-file
  tree against `allowed_writes` in a single pass, instead of today's
  per-file allowlist check, to catch cross-file leakage in one shot.
- **Read-time context graph** -- build the Source Graph/KB/session context
  bundle lazily at the moment a worker actually requests it, rather than
  precomputing and freezing a snapshot at dispatch time.
- **SAFE untrusted-output wrapper** -- wrap worker/model output in a
  structured, explicitly-untrusted envelope before any coordinator surface
  consumes it, instead of treating worker text as directly actionable.
- **Forward-only recovery** -- when a worker's task is interrupted (crash,
  stale lease), recover by replaying forward from the last confirmed
  checkpoint instead of rewinding task state, so a partially completed
  worktree is never silently discarded.

The still-roadmap items above are not scheduled or gated; they remain
candidates to evaluate against the existing dispatch/validation/acceptance
model described above.

## Documentation

- [Getting Started](docs/GETTING_STARTED.md) -- install, Init Repo, first task
- [Architecture](docs/ARCHITECTURE.md) -- how the pieces fit together
- [Publishing](docs/PUBLISHING.md) -- release preflight and tag-driven CI release
- [Changelog](CHANGELOG.md)

## Community

- [Contributing](CONTRIBUTING.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Security Policy](SECURITY.md)
- [License](LICENSE) -- MIT
- [GitHub Issues](https://github.com/shrec/AIWorkHub/issues) /
  [Pull Requests](https://github.com/shrec/AIWorkHub/pulls)
