# Task runtime break-glass — 2026-10-03

Owner authorized a minimal manager repair and reload. This does not finish Manager Chat or release 0.12.18.

Verified repository: `D:\Dev\AIWorkHub`, `repo_be72b4028e3c4e789badc4d5d631d4bd`.
Manager MCP session: `01a0f701-35a9-7141-a63d-861414d9fb7e`.

## Measured blockers and correction

- NF-2026-01247: writable cards with explicit `required_outputs=[]` could not launch on editor workers. Shared normalizer now preserves `[]`, separately from `None`; malformed types, duplicate paths, traversal and out-of-contract outputs remain rejected.
- DeepSeek request `89fb713417124a8398032e43b60df068` retained an obsolete negative test. Rework `ba033bbb497944dfa3138f346d1fc0a0` removed its parameter but retained its id, causing collection failure (14 cases, 15 ids). Both candidates were rejected, not accepted. Manager completed the precise canonical correction through hash-bound semantic edits.
- NF-2026-01248: native Codex request `be2b5021a1ef47019281ea855310d77e` failed `Access is denied.` because the existing resolver understood the old desktop shim, not the official npm forwarding shim. Existing resolver now recognizes only the exact trusted forwarding shape and selects the contained native npm binary. Old launcher, arbitrary shims and explicit overrides retain their existing behavior. Missing targets and unsupported architecture fail closed.
- NF-2026-01249: GLM request `48e3b1818c6f421cb2038fb93fab441f` stopped at `vscode_lm_semantic_edit_stage_required` before any edits or tests; no candidate to accept. Manager implemented the minimal resolver correction under the same self-hosting exception.

## Independent verification

- `tests/test_vscode_lm_bridge.py -n 0`: 84 passed.
- `tests/test_runtime_adapters.py tests/test_os_dependency_boundary.py tests/test_module_size_ratchet.py tests/test_declared_invariants.py -n 0`: 144 passed, 1 skipped.
- Ruff on all four changed Python files and scoped `git diff --check`: passed.
- Exact real-host resolution selects npm's native `codex.exe`; executing `--version` returns `codex-cli 0.159.3`. This is not yet sandbox task-launch evidence.
- `node --test test/package-vsix-version-gate.test.js`: passed.

## Resume after replacement/reload

Re-bootstrap and verify repository/session. Verify the installed resolver and explicit-empty bridge launch; recover the SAME U1 renderer card, preserving its retained predecessor and canonical 0.12.17 version line. Accept only after host affected checks. Then U2, then U3, then build/install 0.12.18 from an external terminal and prove live replies, streaming, callbacks and polling termination. Do not edit the immutable original handoff or launch Claude (quota exhausted). Use Codex, GLM, DeepSeek and OpenCode Muse 1.3 as available. No full extension-suite run (NF1190); affected Node files individually.
