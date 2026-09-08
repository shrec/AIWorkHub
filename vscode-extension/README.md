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

## What's new in 0.11.3 — 2026-09-07

- The system now learns from its own corrections: it reads every rejection it
  has been given, groups them by the rule that was broken rather than the file
  it happened in, and proposes a skill only when the same rule was broken on
  three separate cards.
- A provider hiccup no longer looks like broken code. A transient failure
  retries, an expired credential pauses the lane and keeps your finished work,
  and only a real defect blocks the card.
- A model your account cannot use is switched off the first time it says so.
- The dashboard says "loading" while it is loading, instead of reporting that
  there is nothing there.

