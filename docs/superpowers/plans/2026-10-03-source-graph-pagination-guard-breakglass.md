# NF-2026-01260: Source Graph page identity repair

The activated editor worker infrastructure failed routing request
`0168ea436d11467ab91d21cdd30885f9` before any changed path. Its authenticated
audit ledger records distinct successful query hashes and four continuation
authority mismatches; the final trace labels its discovery ceiling
`duplicate_no_progress`. The ceiling is not proof of identical calls.

An independent probe of the exact production `createVscodeLmSourceGraphGuard`
proved a separate concrete defect: the identity excludes both `cursor` and
`continuation_cursor`. A next page of the same query is replaced with a
duplicate correction before native authority verification.

This is the manager's smallest self-hosting break-glass repair. After U2 request
`2fecac7d67924ada9643e3b570e40eb3` ended with zero changed paths, one prepared,
hash-bound production line was extended with the two cursor identity fields.
No production write overlapped its lease. The new regression test and this
document use the explicit new-file semantic-edit exception.

The fix does not raise discovery or agent-turn limits, bypass cursor authority
verification, relax staging, or authorize additional paths. New pages reach
the existing native verifier; identical page retries still receive correction
and terminate. Distinct pages still consume the original discovery allowance.

## Measured checks

- New `source-graph-pagination-guard.test.js`: five failures before the repair,
  five passes after it; both manager/worker names and both cursor fields, with
  a negative test preserving the discovery ceiling.
- Existing `glm-vscode-lm-bridge.test.js`: pass.
- Existing discovery-transition and finalization-nonprogress harnesses:
  six passes.
- Manager chat panel harness: 53 passes.
- Source Graph/manager MCP continuation tests (`-k continuation`): 23 passes.
  Native cross-authority and stale-cursor protections remain independently
  tested; they are not replaced by the JavaScript identity check.

U2's separate agent-turn-limit failure and index-refresh continuation failures
are not claimed fixed. Installed-code activation and a genuine worker result
are still required before reporting a live repair. No task is accepted merely
because this regression passes.
