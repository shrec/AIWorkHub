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

## What's new in 0.11.13 — 2026-09-09

- Editor-hosted reviewers can submit durable verdicts, with route capability
  telemetry derived from the bridge that actually dispatches the tool.
- Sandbox validation detects unavailable multiprocessing semaphore support
  before valid candidates enter a repeated `PermissionError` loop.
- Kilo/Grok cache and provider-cost telemetry is normalized from real
  `step-finish` events without summing repeated token snapshots.
- Claude's deferred-schema instruction remains Claude-only, saving those prompt
  bytes on Codex, Grok and other worker routes.

## What's new in 0.11.12 — 2026-09-09

- Route telemetry now tells launch readiness, historical evidence, current
  round-trip observation and provider outcome apart.
- Multi-output VS Code LM work stays in semantic staging until every required
  edit/create is complete, then finalizes without another model turn.
- Corrections name the exact missing path/action and repeated refusal fails with
  a bounded semantic-stage error.

## What's new in 0.11.11 — 2026-09-09

- Native Codex workers now receive the full role-scoped AIWorkHub tool contract
  explicitly, including Source Graph, semantic editing and validation.
- A malformed Codex code-worker tool set fails before launch, and Claude-only
  deferred-schema instructions are no longer shown to other transports.

## What's new in 0.11.10 — 2026-09-09

- Retained rework candidates can be rerouted without losing their sealed delta.
- Worker validation and finalization now use the same trusted Python interpreter,
  so an empty or restricted PATH no longer strands a valid task.

## What's new in 0.11.9 — 2026-09-09

- Workers are told which AIWorkHub tool to use instead of each thing they are
  told not to do, and each transport is told what actually enforces its rules
  rather than a claim it could disprove in one turn.
- The raw file editor is no longer handed to workers; the semantic editor does
  the same job with a hash check. Creating a new file is unaffected.
- A worker can declare a genuine exception and have it recorded, and how much
  of a run went through the semantic editor is now measured.
- A relaunch that cannot come out differently is refused, with the legal moves
  named.

## What's new in 0.11.8 — 2026-09-08

- Validation runs work on systems whose Python is a position-independent
  build, which is most of them. The sandbox filter could not be installed
  there and the run died without a message.

## What's new in 0.11.7 — 2026-09-08

- Manager startup no longer leaves a background thread running, which made
  validation runs that fork unstable on smaller machines.

## What's new in 0.11.6 — 2026-09-08

- Tool Recipes now show real usage: which recipes have actually run, who ran
  them, and which are registered but never used. The operator recipes ship with
  the extension, so they work in any repository AIWorkHub manages.
- Reviewers get the diff and the validation output for the lens they were
  launched for, and the automatic reviewer launcher works again.
- A rework worker starts from the failure the previous attempt measured.
- Manager startup sends its contract once per session instead of on every call.
