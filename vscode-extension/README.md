# AIWorkHub VS Code Extension

AIWorkHub opens the operational dashboard as a retained full editor tab through `AIWorkHub: Open Dashboard`. The Activity Bar view is a compact repository launcher only.

## Install

Build the package:

```bash
npm --prefix vscode-extension run package
```

Install the generated VSIX:

```bash
code --install-extension vscode-extension/dist/aiworkhub-0.6.1.vsix
```

Remote-SSH users install it into the remote workspace extension host. The extension kind is `workspace`, so the MCP stdio child and Python runtime execute on the workspace host with no external browser, iframe, HTTP listener, static port, LAN IP, or port forwarding.

## Commands

- `AIWorkHub: Open Dashboard` opens or reveals the stable `aiworkhub.dashboard` editor WebviewPanel.
- `AIWorkHub: Select Repository` is required in multi-root windows before a repository-bound runtime can start.
- `AIWorkHub: Refresh Dashboard` refreshes the visible dashboard panel.
- `AIWorkHub: Restart MCP Connection` replaces the one selected-repository MCP stdio child.

Repository bootstrap creates `.aiworkhub/project.json` and durable `.aiworkhub` state inside the selected repository. Coordinator target routing is stored under `.aiworkhub/config/routing/coordinator-targets.json` and includes provider, repository, window, thread, session, and claim episode identity.
