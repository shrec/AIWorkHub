# Changelog

All notable changes to AIWorkHub are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has
noted by package/extension version and release tag.

## [0.6.1] - 2026-07-21

### Changed

- Removed the runtime dependency on a repository-local `AITools/taskctl.py`:
  the packaged MCP runtime now dispatches task operations directly against
  the selected repository's `.aiworkhub/tasking/task_queue.sqlite` store.
- Added native task verification, review-queue, lifecycle, export, collision,
  callback-outbox, and usage-report compatibility operations.
- Made the VSIX self-contained for initialized repositories and aligned the
  Python package, MCP runtime, and VS Code extension versions at `0.6.1`.
- Added regression coverage proving the installed runtime works without an
  `AITools/` directory or a subprocess call to the legacy task controller.

## [0.6.0] - Unreleased (public release closure)

### Changed

- Removed the manual Model capabilities / GLM canary diagnostics surface
  from the dashboard editor tab (no `vscode.lm.selectChatModels` discovery
  action, no credit-consuming canary prompt) and its obsolete extension-only
  and Python static tests; the real autonomous worker adapters, model
  routing, task launch, and callback behavior are unaffected.
- Python package (`aiworkhub`, `pyproject.toml`) and VS Code extension
  (`vscode-extension/package.json`) versions aligned at `0.6.0`, including
  the extension's own `EXPECTED_MCP_PACKAGE_VERSION` runtime-compatibility
  check.
- README rewritten to open with what AIWorkHub is and a five-minute
  VS Code install/Init Repo/use quickstart, reflecting the current
  repository-local `.aiworkhub/` canonical task-store authority instead of
  the historical `AITools/taskctl.py` / `bitnnv2/data/tasking/*` parent-repo
  wrapper design.
- `docs/ARCHITECTURE.md` and `docs/GETTING_STARTED.md` added.
- `.gitignore` extended to exclude `node_modules/`, `*.vsix`, `*.sqlite`,
  `*.sqlite3`, `*.db`, and repository-local `.aiworkhub/` state.
- `SECURITY.md` and `vscode-extension/package.json` GitHub URLs corrected to
  this repository's actual current remote (`shrec/AIWorkHub`); the
  product/package/CLI identity itself stays canonical `AIWorkHub`/`aiworkhub`.

### Notes

Earlier phases (local stdio MVP, safe local automation, agent launcher,
project-switch readiness, the Task MCP -> Codex callback bridge, VS
Code-owned App Server mux/sideband transport, and the canonical
`aiworkhub` naming cutover) are tracked in detail in `MVP_ROADMAP.md`
rather than duplicated here.

[0.6.1]: https://github.com/shrec/AIWorkHub/releases/tag/v0.6.1
[0.6.0]: https://github.com/shrec/AIWorkHub/compare/v0.5.0...v0.6.0
