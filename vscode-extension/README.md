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
