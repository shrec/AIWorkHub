# Getting Started

This walks through installing AIWorkHub, initializing a repository, and
running your first task, both through the VS Code extension (recommended)
and headless (CLI-only MCP client).

## 1. Install

### VS Code (recommended)

Install
[AIWorkHub from the VS Code Marketplace](https://marketplace.visualstudio.com/items?itemName=IvaneChkheidze.aiworkhub),
or download a versioned VSIX from the
[GitHub Releases page](https://github.com/shrec/AIWorkHub/releases). To build a
development VSIX locally:

```bash
npm --prefix vscode-extension install
npm --prefix vscode-extension test
npm --prefix vscode-extension run package
code --install-extension vscode-extension/dist/aiworkhub-<version>.vsix
```

The extension kind is `workspace`: under Remote-SSH it installs and runs on
the remote host, not locally, so no port forwarding or local Python install
is needed on the client machine. On plain Linux/WSL/Windows the same VSIX
runs unmodified -- there is no host-specific path anywhere in the extension
or the Python runtime. Packaged assets resolve from extension/global storage,
while repository authority always resolves from the active workspace.

### Headless (any MCP-capable client)

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Point any MCP-capable client (Claude Code, Codex, etc.) at
`python3 -m aiworkhub.server` with `AIWORKHUB_REPO=/path/to/checkout` in its
environment (see the `README.md` "MCP Client Config" example).

## 2. Init Repo

AIWorkHub never assumes a repository is already provisioned. The first time
you open the dashboard against a repository, `aiworkhub_dashboard_health`
reports `storage.ready=false` with `not_initialized=true`, and the Webview
shows a single **Initialize AIWorkHub** action. In a headless MCP client,
call the `aiworkhub_dashboard_initialize` tool instead.

Either path calls the same bounded, idempotent bootstrap
(`repository_bootstrap.initialize_repository_full`), which creates/repairs
in the target repository:

- `.aiworkhub/project.json` -- the repository manifest (identity, schema
  version)
- `.aiworkhub/config/storage.json` -- the storage registry
- `.aiworkhub/tasking/task_queue.sqlite` -- a fresh, schema-only canonical
  task queue (never imports or deletes a legacy database)
- `.aiworkhub/sessions/`, `.aiworkhub/memory/`, `.aiworkhub/kb/` -- durable
  Session Manager / AI Memory / KB directories
- the Source Graph database directory

Running Init Repo again on an already-initialized repository is a no-op
that reports what already exists; it never overwrites a manifest with a
mismatched `repo_id`.

## 3. Multi-repository isolation

Every opened repository gets its own `.aiworkhub/` state and its own MCP
stdio child process. Nothing is shared across repositories: switching the
active repository (`AIWorkHub: Select Repository`) stops the previous
child and starts a fresh one bound to the newly selected repository's
`AIWORKHUB_REPO_ROOT`. There is no cross-repository database, cache, or
task queue.

## 4. Source Graph, Session Manager, AI Memory, KB

These four context surfaces live inside each repository's own
`.aiworkhub/` directory and are what a launched worker reads before
touching code:

- **Source Graph** -- repository-local code index queried by
  `aiworkhub_task_show`/worker context bundles instead of ad hoc `grep`.
  Python is AST-indexed. PHP, C/C++/CUDA/OpenCL/Metal, JavaScript/TypeScript,
  Rust, Go, Java and C# have conservative semantic adapters; other registered
  families have truthful file-level evidence. An empty source set reports
  `empty`, not `ready`. See [Source Graph](SOURCE_GRAPH.md).
- **Session Manager** -- a running ledger of the current session so a
  worker (or the next one) can reconstruct exactly what happened.
- **AI Memory** -- durable cross-session knowledge (decisions,
  observations, fixes).
- **KB** -- structured project facts (architecture decisions, invariants,
  known bugs) with FTS5 search.

A code task's worker context bundle reports honest hit counts, bytes, and
hashes for each of these sections rather than claiming injected context was
consumed.

## 5. Run the first task

Open a fresh model chat after initialization so the client performs MCP tool
discovery against the installed runtime. A normal manager loop is:

1. Read repository/preflight state and query Source Graph for the requested
   work boundary.
2. Create one task card with a stable `task_id`, exact objective, acceptance
   criteria, allowed writes, forbidden paths, required outputs, validation and
   optional dependencies.
3. Choose a ready adapter/model from Workforce and launch that exact card.
4. Monitor bounded Live Output or continue other work until the durable
   callback reports a terminal outcome.
5. Review the task's diff, tests, logs, artifacts and authenticated tool-use
   receipts. Accept only verified evidence; otherwise reject with a precise
   residual disposition.

Identical `task_create` retries are safe after an interrupted connection: the
server returns the existing canonical receipt with `created=false`. Reusing a
task id with different content remains an explicit conflict and never
overwrites the first card.

## 6. Task workers and Live Output

`aiworkhub_task_auto_pickup` claims the next pending task for an exact
runner/topic; `aiworkhub_agent_launch_task` starts the configured local
adapter (Claude Code CLI, Codex CLI, or `deepseek_copilot_cli`) against it,
gated behind both `AIWORKHUB_ALLOW_WRITES=1` and `AIWORKHUB_ALLOW_LAUNCH=1`.
The dashboard's task detail view for a selected, currently-running task
shows **Live Output**: a bounded, incremental read of that one task's
stdout/stderr, never a repository-wide log stream.

## 7. Codex callback routing / Claude callback capability

When a claimed task reaches a terminal state (`review_ready`, `blocked`,
`launch_failed`, `validation_failed`, `scope_rejected`, `timed_out`,
`cancelled`), the callback bridge wakes the exact originating Codex thread
automatically -- see the README's
[Callback delivery](CALLBACKS.md) guide for the full
transport/lease/batching contract.

Claude callback delivery reuses the same durable outbox: it uses a
`claude --resume` CLI transport (`ClaudeCliResumeClient`) gated on an
`event_id`/`request_id` acknowledgement echo before a delivery counts as
`delivered`. Known limitation: this transport requires a resumable local
Claude Code CLI session and currently supports the `panel`/`cli_resume`
adapter modes only -- it does not (yet) speak the VS Code-owned App Server
sideband transport that the Codex path uses for `transport="sideband"`.

## 8. Operations and troubleshooting

The dashboard's **Operations** dialog contains KPIs, Tool Use, Storage,
workforce and reliability evidence. Use the bounded last-log row and Logs
viewer before reading host logs directly.

- **Connecting:** run **AIWorkHub: Restart MCP Connection** once. It replaces
  only AIWorkHub's selected repository child.
- **Chat has no tools:** create a new chat after install/upgrade so MCP
  discovery runs against the current server.
- **Windows has an old `PYTHONPATH`:** current releases migrate AIWorkHub-owned
  Codex config from source/version paths to the packaged host-stable launcher
  automatically. Tool approval subsections are preserved.
- **Model unavailable:** inspect Preflight, authenticate that provider and
  grant VS Code's one-time model consent if requested.
- **Source Graph empty:** verify the repository was initialized, enable the
  relevant language family in Settings and refresh the index.

## 9. Running the tests

```bash
python -m pytest tests/ -v
npm --prefix vscode-extension test
npm --prefix vscode-extension run package
```

See [Publishing](PUBLISHING.md) for the full release preflight and
[Architecture](ARCHITECTURE.md) for how these pieces fit together.
