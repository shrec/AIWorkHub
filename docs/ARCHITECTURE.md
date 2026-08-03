# Architecture

## Overview

AIWorkHub is a repository-bound control plane. Every unit of durable state
(task queue, sessions, memory, KB, Source Graph, routing config) lives under
one repository's own `.aiworkhub/` directory. There is no central server, no
shared database, and no multi-repository knowledge service -- each opened
repository is a fully independent instance.

```text
                 ┌───────────────────────────────┐
                 │   VS Code extension host       │
                 │  (vscode-extension/extension.js)│
                 │  - Activity Bar repo launcher   │
                 │  - editor-tab Webview dashboard │
                 └───────────────┬────────────────┘
                                  │ stdio (one MCP child per repo)
                                  ▼
                 ┌───────────────────────────────┐
                 │   aiworkhub.server (FastMCP)    │
                 │  ~30 read-only + write-gated    │
                 │  tools; dashboard_mcp_app.py's  │
                 │  narrow snapshot/detail/health/  │
                 │  live-output/initialize surface │
                 └───────────────┬────────────────┘
                                  │
      ┌───────────────┬──────────┼───────────┬───────────────────┐
      ▼               ▼          ▼           ▼                   ▼
 task_store.py   source_graph.py  process_launcher.py  callback_bridge.py
 (.aiworkhub/     (.aiworkhub/     (isolated worker      (Codex App Server /
  tasking/         source_graph/)  workspaces, adapters:  Claude CLI resume
  task_queue                       Claude/Codex/DeepSeek) transport, durable
  .sqlite)                                                outbox + lease)
```

## Repository-local authority (`.aiworkhub/`)

`repository_state.py` resolves the active repository root from
`AIWORKHUB_REPO_ROOT` (the env var the extension host spawns the MCP child
with) or the process cwd -- never a fixed path relative to this package's
own install location, so the same install works identically for any
checkout on Linux, WSL, or Windows.

`task_store.py` owns the canonical schema:

- `.aiworkhub/project.json` -- manifest (repo identity, schema version)
- `.aiworkhub/config/storage.json` -- storage registry
- `.aiworkhub/tasking/task_queue.sqlite` -- the canonical task queue
- `.aiworkhub/sessions/`, `.aiworkhub/memory/`, `.aiworkhub/kb/` -- Session
  Manager / AI Memory / KB durable directories

`repository_bootstrap.py` sequences `task_store.initialize_repository` and
`source_graph.resolve_db_path` into one idempotent Init Repo action (see
[Getting Started](GETTING_STARTED.md)); it never writes a manifest or
schema itself.

`storage_registry.py` / `task_store_migration.py` / `fresh_task_store.py`
handle registry verification and the one-way cutover from any legacy
JSONL/SQLite layout to the canonical schema -- a fresh repository never
re-imports historical rows.

`task_retention.py` owns archived-task cleanup. It selects only archived rows
older than the repository policy threshold, excludes tasks whose callback is
still pending or in flight, and binds the preview to the exact candidate list
with a digest. Confirmed cleanup first copies the task, events and callback
records into canonical quarantine tables. Restore is available for seven days
and refuses identity collisions; expired payload purge is a separate action,
while the compact retention audit remains durable. No dashboard action deletes
an active, processing or review task through this lifecycle.

## MCP server surface

`server.py` wires the read-only and write-gated tool sets over
`core.py` (task health/list/show/claim/review/done wrappers),
`completion_inbox.py`, `cost_ledger.py`, `stale_recovery.py`,
`cli_adapter_dryrun.py` / `cli_adapter_readonly_tool.py`, and
`worker_ai_tools_mcp.py` / `agent_tool_instructions.py` (the bounded
Source Graph / KB / AI Memory / Session context surface a launched worker
reads before touching code). Two explicit environment gates protect every
mutation:

```bash
AIWORKHUB_ALLOW_WRITES=1   # queue mutations (auto-pickup, mark-review, ...)
AIWORKHUB_ALLOW_LAUNCH=1   # real local process launch
```

Both default to `0`; a launched worker never inherits the launch gate from
its parent.

## Dashboard: native Webview, not HTTP

`dashboard.py` exposes pure read-only builders (`build_snapshot`,
`build_task_detail`) over the providers above. `dashboard_mcp_app.py` wraps
them in a bounded MCP tool surface
(`aiworkhub_dashboard_snapshot`/`_task_detail`/`_health`/`_task_live_output`/
`_initialize`) that the VS Code Webview calls over the same repository-local
stdio session -- there is no in-package HTTP listener, browser launch, or
fixed listen port anywhere in the runtime.

## Process launch and isolation

`process_launcher.py` starts a configured adapter (Claude Code CLI, Codex
CLI, or `deepseek_copilot_cli` via `runtime_adapters.py` /
`deepseek_credentials.py`) shell-free, in its own process group, with
PID/timeout/cancel tracking and an append-only process-event log.
`worker_workspace.py` provisions an isolated worker workspace (Landlock
sandbox where available) so a launched worker's Source Graph queries and
writes stay bounded to its declared `allowed_writes`.

The preferred credential-free editor route is
`vscode_lm_bridge.py` + `VscodeLmBridgeHost`. The extension publishes a
repo/window-scoped heartbeat containing only bounded public model metadata;
no credential enters the repository or Python worker. A task request is
spooled owner-only to the extension host, which invokes the exact visible
model through `vscode.lm`, proxies bounded worker tools, and returns a strict
edit envelope to the isolated workspace. VS Code's extension API carries this
model surface across Remote-SSH: repository state and validation remain on the
remote workspace host while authorization/consent remains owned by the editor
provider. Preflight distinguishes no host, stale host and a genuine live-host
model miss, and workforce routing propagates the effective adapter selected
from that evidence into the launch decision.

## Callback bridge

`callback_bridge.py` closes the loop back to the coordinator: when a
claimed task reaches a terminal state, a durable, deduplicated
`callback_outbox`/`callback_batches` entry in the repository-local canonical
task store wakes the verified manager through its available transport. Codex
can use manager-inbox delivery or the optional same-host App Server sideband;
Claude can use its manager inbox, native channel or resumable CLI transport.
See [Callback delivery](CALLBACKS.md) for the full lease/retry/batching contract
and compatibility boundary, and
[Getting Started](GETTING_STARTED.md#6-codex-callback-routing--claude-callback-capability)
for the current Claude-transport capability/limitations summary.
`app_server_mux.py` is the VS Code-owned App Server sideband transport used
for the Codex path when configured; the Claude transport does not use it.

## Source Graph / Session Manager / AI Memory / KB

`source_graph.py` / `source_graph_ast.py` / `source_graph_languages.py` /
`source_graph_insights.py` / `source_graph_migration.py` index the repository's
own code into `.aiworkhub/source_graph/` so worker
context bundles and the `aiworkhub_cli_adapter_*` tools can answer
structural queries without `grep`. `project_context.py` /
`context_cache.py` / `context_economics.py` assemble and cache the bounded
Source Graph + Session + AI Memory + KB bundle a code task receives, and
report requested-vs-executed hit counts, bytes, and hashes rather than
claiming injected context was consumed.

`process_launcher.build_worker_prompt` applies a second envelope around that
bundle: exact byte ceilings for coordinator context, task contract and total
prompt; compact canonical contract serialization; and separate initial versus
residual-rework limits. A rework card points at a retained, hash-pinned
predecessor workspace and carries only bounded feedback and residual identities.
The process ledger records the prompt section byte breakdown so dashboard KPI
comparisons can be measured without presenting byte estimates as token truth.

Authenticated worker receipts feed both Source Graph-specific economics and a
generic per-tool ledger. Dashboard aggregation therefore distinguishes calls,
successful calls, bounded bytes and cache hits for Source Graph, Session
Manager, AI Memory, KB and other MCP tools without treating a prompt-time
injected bundle as continuous use.

## VS Code extension

`vscode-extension/extension.js` owns exactly one thing outside the MCP
protocol: routing. It selects the active repository, spawns/restarts the
one MCP stdio child bound to it, and proxies a fixed, validated message
enum between that child and the Webview (`vscode-extension/media/app.js`)
-- the Webview never receives a coordinator token, an environment value, a
filesystem path, or an arbitrary tool-call primitive. The bundled VSIX
packages the `aiworkhub` Python runtime under an extension-local `runtime/`
directory (`vscode-extension/test/package-vsix.js`), so installing it
requires no repository checkout, editable install, or network-time package
install.
