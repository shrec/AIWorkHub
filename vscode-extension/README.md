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

## What's new in 0.11.4 — 2026-09-08

- Python and Node validation commands can update timestamps on their permitted
  scratch files while the sandbox continues to enforce file ownership and paths.
- Windows process supervision and file handling use shared structure definitions
  to keep their platform behavior consistent.
- Rejected successful work retains its verified candidate so corrections can
  continue from the same attempt.
