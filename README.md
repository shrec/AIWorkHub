# GeoAI Task MCP

Single-project MCP orchestrator and local operations dashboard for GeoAI.

The first version intentionally reuses the existing parent-repo task system:

- Source of truth: `AITools/taskctl.py`
- Queue DB: `bitnnv2/data/tasking/task_queue_v1.sqlite`
- MCP transport: stdio
- Default mode: read-only and launch-disabled
- Local model adapters: Claude Code CLI, Codex CLI, and `deepseek_copilot_cli`
  (official GitHub Copilot CLI driven in BYOK mode against DeepSeek's
  OpenAI-compatible API)
- DeepSeek adapter: `deepseek_copilot_cli` is the local-launch adapter for every
  `deepseek_*` runner; `deepseek_manual` remains an explicit non-launchable
  fallback only

## Install For Local Development

From this directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

If the parent environment already has `mcp` installed, direct execution also works:

```bash
PYTHONPATH=src GEOAI_REPO=/home/shrek/GeoAI python3 -m geoai_task_mcp.server
```

## MCP Client Config

Example MCP server entry:

```json
{
  "mcpServers": {
    "geoai-task-mcp": {
      "command": "python3",
      "args": [
        "-m",
        "geoai_task_mcp.server"
      ],
      "env": {
        "PYTHONPATH": "/home/shrek/GeoAI/tools/geoai-task-mcp/src",
        "GEOAI_REPO": "/home/shrek/GeoAI",
        "GEOAI_TASK_MCP_ALLOW_WRITES": "0"
      }
    }
  }
}
```

Set `GEOAI_TASK_MCP_ALLOW_WRITES=1` only for trusted local automation. A real
model launch additionally requires `GEOAI_TASK_MCP_ALLOW_LAUNCH=1`; both gates
default to `0`. Launches are local, shell-free, exact-task-bound, process-group
tracked, timeout-bounded, and collision-checked.

## Exposed Tools

Read-only by default:

- `geoai_task_health`
- `geoai_task_review_queue`
- `geoai_task_list`
- `geoai_task_show`
- `geoai_task_pending_for_runner`
- `geoai_task_collision_guard`
- `geoai_task_usage_report`
- `geoai_task_audit_log_read`
- `geoai_task_completion_inbox`
- `geoai_task_stale_recovery_recommend`
- `geoai_task_cost_ledger`
- `geoai_agent_task_status`
- `geoai_agent_collect_result`
- `geoai_agent_list_processes`
- `geoai_cli_adapter_plan_readonly`
- `geoai_cli_adapter_audit_summary_readonly`
- `geoai_cli_adapter_report_readonly`

Write-gated:

- `geoai_task_auto_pickup`
- `geoai_task_mark_review`
- `geoai_task_reject_review` (Codex requeues a failed review with feedback)
- `geoai_task_mark_done`
- `geoai_task_export_jsonl`
- `geoai_task_queue_request`
- `geoai_agent_launch_task` (requires both write and launch gates)
- `geoai_agent_cancel_task` (requires both gates)

`geoai_task_queue_request` is the legacy intent/audit API and still launches
nothing. `geoai_agent_launch_task` is the real runtime API. It validates the
exact pending task, runner/topic, allowed-write boundary, collision state,
adapter, and concurrency cap before starting a process. The worker's first
operation is exact `taskctl claim-start <task_id>`, and success is not reported
as complete until the task reaches `review`.

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
(default `~/.config/geoai-task-mcp/deepseek_copilot_credential.json`, override
with `GEOAI_TASK_MCP_DEEPSEEK_CREDENTIAL`). The key is read via `getpass` (never
on a command line) and is loaded only on the coordinator side at launch time. It
enters **only** the launched child process as `COPILOT_PROVIDER_API_KEY` — never
argv, task cards, logs, audit events, dashboard payloads, or Git.

```bash
geoai-task-deepseek-credential set        # prompts for the key (hidden input)
geoai-task-deepseek-credential status     # secret-free readiness JSON
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
`geoai_completion_inbox` MCP tool output and in the dashboard's
`adapter_readiness` snapshot section (folded into existing surfaces so the
frozen v1 tool contract stays at 33 tools). Per adapter it reports `installed`,
`credential_present`, `endpoint`, `supported_models`, `launchable`, and the
exact non-secret `blocker_reason`. Credential contents and hashes are never
exposed. `geoai-task-deepseek-credential status` prints the same secret-free
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
`GEOAI_CALLBACK_APP_SERVER_TIMEOUT_SECONDS` /
`GEOAI_CALLBACK_LEASE_SECONDS` / `GEOAI_CALLBACK_MAX_BATCH_MEMBERS` env
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
geoai-task-callback-bridge run-once      # process at most one pending BATCH, then exit
geoai-task-callback-bridge daemon        # poll continuously (idle polling starts zero
                                          #   Codex/model processes and uses zero tokens)
geoai-task-callback-bridge status        # redacted health: bound/unbound, per-state
                                          #   outbox+batch counts, batch size, oldest
                                          #   pending age, last delivery -- never a full
                                          #   thread id
geoai-task-callback-bridge dry-run TASK_ID STATE
                                          # disposable canary: builds the prompt/argv,
                                          # never starts a real App Server subprocess

# All three accept --app-server-timeout-seconds/--lease-seconds/--max-batch-members
```

**systemd (user) example for automatic restart** --
`~/.config/systemd/user/geoai-task-callback-bridge.service`:

```ini
[Unit]
Description=GeoAI Task MCP callback bridge (Codex App Server outbox consumer)

[Service]
Type=simple
Environment=GEOAI_REPO=%h/GeoAI
WorkingDirectory=%h/GeoAI
ExecStart=%h/GeoAI/.venv/bin/geoai-task-callback-bridge daemon
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now geoai-task-callback-bridge.service
```

Uses the existing local Codex authentication (the `codex` CLI's own login);
the bridge stores no API/access token of its own.

## App Server Mux + Sideband Transport

The sideband bridge must wait until the VS Code extension has been reloaded
through the configured mux.  Its lease must also cover the 45-second App
Server deadline plus the 300-second recovery margin (minimum 345 seconds).

A separately spawned bridge App Server can only ever see the VS Code OpenAI
extension's owned thread from the outside: the extension spawns and owns its
own `codex app-server` child over a private stdio pipe, and `thread/resume`/
`turn/*` are scoped to the App Server INSTANCE that owns the turn, not the
thread id in the abstract. `geoai_task_mcp/app_server_mux.py` closes this
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
python3 tools/geoai-task-mcp/scripts/install_vscode_app_server_mux.py --check
python3 tools/geoai-task-mcp/scripts/install_vscode_app_server_mux.py
```

Codex owns applying the printed `chatgpt.cliExecutable` value, reloading the
extension host, the live canary against the extension-owned thread, and
finally switching the callback bridge to `transport="sideband"` and
re-enabling its service.

## VS Code Dashboard

Use the repository-local **AIWorkingHub** VS Code extension. Its native Webview
reads the canonical dashboard snapshot through a repository-local Task MCP
stdio session. It does not open a browser, expose a LAN address, start an HTTP
listener, or require port forwarding. Installation and operation are documented
in `tools/vscode-geoai-task-operations/README.md`.

Task-bound AI context telemetry is metadata-only. Source Graph, Session
current-state, AI Memory, and KB sections report requested/executed state, hit
counts, bytes, hashes, truncation, and degraded reasons. The dashboard shows
context injected versus worker-acknowledged separately; injected context is not
treated as consumed unless the worker emits the bounded
`geoai.task_mcp.worker_context_receipt.v1` receipt. Raw context bundle content
is not stored in process events or dashboard payloads.

Context policy is task-type aware. Code tasks require non-empty Source Graph
evidence unless a card explicitly marks Source Graph non-gating. Data
classification tasks may use an immutable input shard as the primary context,
with empty Source Graph reported honestly as zero hits rather than as token
savings. Raw-context-versus-bundle byte reporting is a deterministic estimate,
not a token or cost claim.

## Runtime Gates

```bash
export GEOAI_TASK_MCP_ALLOW_WRITES=1
export GEOAI_TASK_MCP_ALLOW_LAUNCH=1
export GEOAI_TASK_MCP_MAX_PROCESSES=4
```

Keep both gates disabled for worker-facing MCP configurations. Enable them only
in the coordinator's local MCP process. A launched worker does not inherit the
launch gate.

## Write-Gate Audit Log

Every blocked write attempt is recorded to an append-only JSONL audit log.

**Default path:** `tools/geoai-task-mcp/logs/audit.jsonl` (relative to `GEOAI_REPO` root).

**Override path:** set `GEOAI_TASK_MCP_AUDIT_LOG_PATH` to an absolute path.

**Format:** one JSON object per line:

```json
{"timestamp":"2026-07-04T12:00:00+00:00","tool_name":"auto-pickup","action":"blocked_write","blocked_reason":"...","caller_info":{"pid":12345,"env_vars_checked":{"GEOAI_TASK_MCP_ALLOW_WRITES":"<set>","GEOAI_TASK_MCP_AUDIT_LOG_PATH":"<unset>"}}}
```

**Safety guarantees:**
- Never logs secret values, tokens, passwords, or environment variable VALUES — only NAMES and `<set>`/`<unset>` status.
- Append-only — existing entries are never overwritten.
- Log write failures print a warning to stderr but never crash the server.
- `GEOAI_TASK_MCP_ALLOW_WRITES` default remains `0` (off) — the audit log does not enable writes.

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
