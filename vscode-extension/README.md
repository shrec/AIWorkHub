<div align="center">
  <img src="media/aiworkhub-hero.svg" alt="AIWorkHub" width="100%">
</div>

# AIWorkHub for VS Code

AIWorkHub is a repository-native control plane for multi-model software
development. It gives every repository an isolated task system, Source Graph,
durable project context, worker runtime and evidence-first review loop.

The extension opens as a retained editor tab and runs one repository-scoped
MCP stdio runtime on the workspace host. It does not open a browser, bind a
port, expose a LAN service or require an AIWorkHub cloud account.

## Highlights

- Plan and inspect dependency-aware AI tasks from one operational dashboard.
- Delegate to supported local model adapters and track real terminal outcomes.
- Replace repeated raw-source discovery with a repository Source Graph.
- Preserve continuity through Session Manager, AI Memory and KB.
- Review diffs, tests, logs, artifacts and approval history before acceptance.
- Keep repositories isolated in separate `.aiworkhub/` authorities.
- Run on Linux, macOS, native Windows, WSL and Remote-SSH.

## Get started

1. Install the VSIX and open a Git repository in VS Code.
2. Run **AIWorkHub: Open Dashboard**.
3. Select the repository when using a multi-root workspace.
4. Choose **Initialize AIWorkHub** on first use.
5. Open a new Codex, Claude or MCP-capable chat after registration so the new
   runtime tools are discovered by that chat process.

Initialization is explicit and idempotent. It creates repository-local state
only under `.aiworkhub/` and starts the first Source Graph index in the
background.

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

## Trust and privacy

- Local stdio transport; no AIWorkHub network listener.
- Read-only and launch-disabled by default.
- Repository-specific state, route identity and audit trail.
- No AIWorkHub telemetry upload of prompts, source, credentials or memories.
- Explicit manager authority for context writes and task acceptance.

Read the full [Getting Started guide](https://github.com/shrec/AIWorkHub/blob/main/docs/GETTING_STARTED.md),
[Architecture](https://github.com/shrec/AIWorkHub/blob/main/docs/ARCHITECTURE.md),
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
