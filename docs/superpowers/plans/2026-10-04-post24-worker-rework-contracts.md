# Post-0.12.24 worker rework contracts

Canonical repository: `D:\Dev\AIWorkHub`,
`repo_be72b4028e3c4e789badc4d5d631d4bd`.
This manager brief supplements the exact existing cards; it does not waive gates,
authorize writes outside those cards, or move sealed candidates between cards.

## Activation gate

Release 0.12.24 / commit `183bb0d` is installed. A fresh heartbeat from bound
window `window_7a9486caf893d6cef874215a`, live PID 33092, still reported extension
0.12.23 at 2026-10-03T22:07:17.122Z, with zero active requests. Recheck this actual
heartbeat after owner Reload Window before a paid editor-worker rework. A disk
installation or fresh Python MCP client is not proof of new Node bridge activation.
Do not kill or restart a worker merely because an observation times out.

## NF1319: preserve the outcome authority boundary

Existing task `AIWORKHUB_NF1319_CURRENT_TERMINAL_FAILURE_LEARNING_DEEPSEEKFLASH_V1`.
Rejected current request `e6d5f0d4ccda4b76bb49cef809b77102`, claim 8, seal
`7bc5ffa8e56fde65214b011155226081a7792a075b79eaeb106f2989672eab18`.
Original allowed writes: learning_commit_store.py and its existing test file.

The exact corrected line 1004 has **four** leading spaces:
`elif not _request_matches_candidate(card, request_id, readiness):`.
Its raise at line 1005 retains eight spaces. This branch is a sibling of the
accepted/rejected outcome branches; indenting the raise further would remove
the inconclusive-outcome guard. The generic parser finding in the first reject
is not permission to make that semantic regression.

The sanitized worker input itself contained eight spaces (sequence 56); the
worker then supplied the correct four-space input (60), rejected by the old
collector with range_conflict (61). NF1322 repairs same-run recovery through a
fresh bounded Source Graph body and authenticated native prepare/apply; it never
permits a blind overwrite. Require current terminal-failure task/repo/request/
claim/envelope/storage binding, no promotion on inconclusive, accepted/rejected
manager proof, and real manager-API negative no-write/idempotent-positive tests.
Keep all four original validation commands and both output dispositions.

## NF1307: imports alone are not the production fix

Existing task `AIWORKHUB_NF1307_REPOLOCAL_NO_DOS_DRIVE_COMPLETE_CONTRACT_GLM53_V3`.
Rejected DS Pro request `645403735eb94833b126ad176f9e1d4e`, claim 12, seal
`afcd3ede85164e30549853d423f9e8e7025c9c22d7c8b07df2374f44bccc9bef`.
All three original production/test outputs remain in scope.

New no-drive tests omit build_command_line and snapshot_filesystem_acl imports;
the standalone live probe fails before it exercises any sandbox. Correct those
names without weakening denial, revocation or failure-unwind assertions.

A readonly diagnostic of the real _request_traversal_anchor selected the nearest
nested worktree root, 140 characters. Its 162-character helper leaf projects to
a 320-character nested LSP cwd for username shrek, exceeding the actual 258 limit.
The outer canonical repo-local worktrees anchor is 45 characters and projects
to 225 with the same helper shape. These are path measurements, not authority
or native sandbox proof; no directory, grant or process was created by the check.

Do not simply select an outer lexical ancestor. A usable short allocation root
must be authenticated by the owning request and its canonical repo authority,
not guessed from cwd, .git, a drive letter or an environment string. Use a unique
request-owned revocable leaf inside .aiworkhub, with an explicit traversal boundary.
If carrying that authority requires another production caller or fixture, obtain
the complete atomic native card scope before editing it. Source Graph confirms
launch_appcontainer calls bind_validation_helper_temp at line 1990; trace that
production boundary, not merely the new test.

Require no DOS aliases, administrator rights or profile/drive-root grants;
unowned roots, nested lookalikes, root escapes and junctions fail closed. Retain
grant-failure unwind, sibling isolation and all six existing gates. Actual native
read/write denial and grant revocation must pass before claiming OpenCode/Muse ready.

## NF1305: complete the literal asset closure within the contract

Existing task `AIWORKHUB_NF1305_JS_LITERAL_ASSET_SEED_GLM53_V1`.
Rejected request `fa42b406e05d4a629f06165d08badf60`, claim 8, seal
`0f35cc2818134642a872b96a6c96e3a1bacc6a787fd65f27447e17f018dd56de`.

The replacement at test line 10239 repeated an already-open call instead of
closing it; lines 10237-10241 cannot parse. The source has 14302 lines against
the unchanged 14148 ratchet, and actual Node bridge nf1275VisibleActivityEndToEnd
still fails null != 0 at 6887. Other green targets are not end-to-end evidence.

Require literal direct reads and finite literal-array path.join closure, wired
to the production seeder, with tracked/private/symlink/MAX_SEED/cache guards.
Do not raise the ratchet, delete unrelated code or assertions, compress arbitrary
old code, or pretend the helper exists because its call site does. If a new small
helper module is necessary, obtain an atomic native scope including the module,
production import/call site and packaging/closure regression fixtures it affects;
the old two-file card alone does not authorize that extra file. Preserve the five
original gates, including the real Node bridge harness.

## Manager Chat remains a live acceptance requirement

Native status currently has no active manager conversation, no running turn and
no queued/in-flight wake. Returned saved conversations are closed; retain their
transcripts, rather than deleting history merely because it is closed. The
15 callback backlog entries are separate evidence and not proven cleared by this
status. No Claude launches. Do not steal the current manager route for a live
panel seat while worker/reviewer work is running.

Accepted U1/U2/U3 and green baseline/host harnesses do not replace M1 live replies,
streaming latency, callback delivery, polling termination, final-text capture,
measured context rotation or explicit goal terminal semantics. OpenCode/Muse
sandboxed MCP/model response, uniform context/cost policy and skills' real use,
accepted outcome and actor attribution remain in the full goal.
