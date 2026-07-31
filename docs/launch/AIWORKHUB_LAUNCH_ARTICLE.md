# Introducing AIWorkHub: a repository-native control plane for AI coding agents

> **Suggested canonical URL:** https://github.com/shrec/AIWorkHub
>
> **Meta description:** AIWorkHub is an open-source, local-first VS Code control
> plane for planning, delegating and reviewing software-development tasks across
> multiple AI coding models without giving up repository authority.

AI coding tools are increasingly capable, but coordinating more than one of
them still feels improvised. A task is copied into a chat, context is scanned
again, a worker changes files, and the result is accepted because the model
says it is finished. The conversation, evidence and project state are often
scattered across tools.

I built **AIWorkHub** to turn that workflow into a repository-owned engineering
system.

AIWorkHub is an open-source, local-first control plane for multi-model software
development in VS Code. It gives each Git repository its own task queue, code
index, durable context, worker isolation, evidence bundles and manager review
loop. It works with model routes already available through VS Code or their
authenticated CLIs, rather than moving project authority into an AIWorkHub
cloud service.

The project is available under the MIT License:

**https://github.com/shrec/AIWorkHub**

## The problem is coordination, not another chat box

Once several coding agents participate in a project, the hard questions are no
longer only about generation quality:

- Which task is ready, and which one is blocked by another change?
- Which model is appropriate for the work and currently available?
- What files may a worker modify?
- Did the worker use the repository's approved context tools throughout the
  task, or fall back to repeatedly scanning the tree?
- Which tests, diffs and artifacts support the claimed result?
- Who accepts the result and updates canonical project state?
- How do callbacks return to the correct manager, repository and chat?
- What survives a reload, a long session or context compaction?

AIWorkHub treats these as control-plane concerns. The model remains useful for
reasoning and implementation, while deterministic software owns identity,
routing, state transitions, isolation and evidence.

## One isolated workspace per repository

After explicit initialization, AIWorkHub creates a repository-local
`.aiworkhub/` state directory. Tasks, callback records, indexes, sessions,
memories, knowledge entries and audit evidence belong to that repository.

There is no shared project database and no AIWorkHub HTTP service. The VS Code
extension communicates with a repository-bound MCP runtime over stdio. This
keeps two projects open in two editor windows from silently sharing task state
or context.

The same verified manager chat can switch between live repositories by stable
repository identity. The selected repository changes; the manager thread does
not need to be recreated for every project.

## A task lifecycle with a real review boundary

AIWorkHub models work as dependency-aware task cards. A card can declare:

- its objective and acceptance criteria;
- the runner and model route;
- allowed write paths and forbidden actions;
- dependencies and collision constraints;
- required outputs and validation commands.

Workers run in bounded workspaces. A completed process does not silently become
an accepted change: it enters review with its terminal status and evidence. The
manager can inspect the diff, tests, logs, artifacts, validation history and
tool-use receipts, then accept the change or return a precise residual for
rework.

That distinction matters. "The agent finished" and "the repository accepted
the change" are different events.

## Source intelligence instead of repeated tree scans

AIWorkHub maintains an automatically refreshed **Source Graph** for the active
repository. Managers and workers can request bounded structural context instead
of repeatedly rediscovering the codebase with broad filesystem scans.

The goal is not to publish an unverifiable "tokens saved" number. AIWorkHub
records context evidence: what was requested, what was delivered, what the
worker acknowledged, whether content was truncated and why a route degraded.
This makes context efficiency measurable without pretending that bytes are
automatically tokens or dollars.

Source Graph usage is part of the task evidence, so teams can measure whether
agents use the intended code-intelligence path throughout their work rather
than only mentioning it at the start of a prompt.

## Durable context has more than one job

AIWorkHub deliberately separates several context authorities:

- **Session Manager** holds current execution state, checkpoints and handoffs.
- **AI Memory** stores reusable decisions and lessons with lifecycle state.
- **Knowledge Base** stores curated project contracts and authoritative facts.
- **Source Graph** provides bounded structural code intelligence.
- **Manager Context Graph** optionally preserves exact completed manager-chat
  evidence and deterministic links between repositories, threads, sessions,
  tasks, actors and events.

The Context Graph is a recovery layer, not an automatic policy engine. A past
conversation can explain why a decision was made; it does not silently override
the current knowledge base or task contract. Current passive capture supports
completed Codex user and assistant messages. It excludes reasoning, streaming
deltas, tool output, commands and approval prompts.

## Multi-model routing without copying credentials

AIWorkHub can route work to Codex, Claude, DeepSeek, GLM and models exposed by
the VS Code Language Model API, depending on what is installed and authorized
on the user's machine.

Editor routes use models already visible in VS Code and ask for the editor's
normal one-time model consent. CLI routes reuse that CLI's authenticated
session. AIWorkHub does not copy editor or CLI credentials into the repository.

Availability is discovered at runtime because subscriptions and model catalogs
differ. A preflight surface reports which adapters are installed, authorized
and launchable before a task is claimed.

## Safety is part of the architecture

Autonomous coding needs narrow authority. AIWorkHub includes:

- separate write and process-launch gates, both disabled by default;
- shell-free exact-task launches;
- explicit allowed-write scopes;
- isolated worker workspaces and Landlock where available;
- owner-only credentials outside repositories;
- secret-redacted logs;
- durable callback delivery with lease, retry and deduplication;
- fail-closed repository, manager, task and claim identity checks;
- append-only audit evidence and authenticated tool-use receipts.

The manager keeps acceptance authority. Launched workers do not inherit the
manager's ability to launch more workers.

## The dashboard

The AIWorkHub VS Code dashboard exposes the task queue, dependency state,
review inbox, live output, callback health, adapter preflight, storage usage,
Source Graph status and repository context stores. It opens as an editor tab so
the orchestration surface can sit beside code and model chats.

![AIWorkHub dashboard](../assets/screenshots/aiworkhub-self-hosted-dashboard.png)

AIWorkHub is also self-hosted in the practical sense: its own development can
be planned, delegated and reviewed through an AIWorkHub repository instance.

## Install it

The current public distribution channel is a GitHub Release VSIX. Marketplace,
Open VSX and PyPI publication are planned but are not live yet.

1. Download `aiworkhub-*.vsix` from the
   [latest release](https://github.com/shrec/AIWorkHub/releases/latest).
2. Install it:

   ```bash
   code --install-extension aiworkhub-*.vsix
   ```

3. Open a Git repository in VS Code.
4. Run **AIWorkHub: Open Dashboard**.
5. Choose **Initialize AIWorkHub** once.
6. Open a new model chat so it discovers the repository MCP tools.

The packaged extension is qualified for Linux, macOS, native Windows, WSL and
Remote-SSH. Initialization is explicit and idempotent; it creates the local
stores and starts the first Source Graph index.

## What comes next

The engineering foundation is in place, but this is still an early open-source
release. The near-term work is focused on distribution, provider-independent
callback qualification, richer visual planning, historical reliability
analytics and making context economics easier to inspect.

If this direction is useful to you, try the release on a non-critical
repository, read the security model before enabling launches, and share the
rough edges you find.

**Repository:** https://github.com/shrec/AIWorkHub  
**Releases:** https://github.com/shrec/AIWorkHub/releases  
**License:** MIT

Thanks to [null0xxx](https://github.com/null0xxx) for sharing
[kimi-atlas](https://github.com/null0xxx/kimi-atlas) and useful ideas about
multi-agent orchestration and evidence-driven verification.

