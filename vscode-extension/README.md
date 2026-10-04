<div align="center">
  <img src="https://raw.githubusercontent.com/shrec/AIWorkHub/main/vscode-extension/media/aiworkhub-hero.png" alt="AIWorkHub" width="100%">
</div>

# AIWorkHub for VS Code

**Plan. Delegate. Verify. Remember.**

AIWorkHub is a repository-native control plane for multi-model software
development. It gives every repository an isolated task system, Source Graph,
durable project context, worker runtime and evidence-first review loop.

The extension opens as a retained editor tab and runs one repository-scoped
MCP stdio runtime on the workspace host. It does not open a browser, bind a
port, expose a LAN service or require an AIWorkHub cloud account.

## What's new in 0.12.25

- Windows AppContainer validation commands get the request's own temp directory instead of the adapter-shared `AC\Temp`: the exec scratch lives under the request home and non-Python commands run under a trampoline that restores TEMP/TMP (NF-2026-01341).
- The recorded command stays the declared argv and exit codes are preserved. Includes the 0.12.24 same-range editor-correction recovery.
- Component regressions and an opt-in live AppContainer probe pass; installation and live worker replay are separate evidence.

## What's new in 0.12.23

- Windows Source Graph writer contention uses a verified process-creation identity through the platform interface; PID-only or unknown identity stays fenced.
- Foreign-process signalling, private ownership, atomic publication and existing safety gates are unchanged.
- Canonical OpenCode executable binding and AST closure caching are included. Full sandboxed Muse/MCP startup and Manager Chat end-to-end qualification remain unclaimed.

## What's new in 0.12.22

- Explicitly allowed optional outputs can be staged alongside mandatory outputs; mandatory completion is not a second write scope.
- Exact scope/action contracts, semantic hashes/ranges, substantive content and completion checks stay enforced.
- Independent component regressions pass. Installation/live replay and full Manager Chat, sandboxed OpenCode/Muse, context policy and Skills completion are separate, unclaimed checks.

## What's new in 0.12.21

- Authorized complete-create worker stages have a separate 255 KiB payload bound; general requests retain 16 KiB.
- Card scope, payload fidelity, semantic edit authorization and mandatory-output gates remain enforced. Oversize diagnostics report the selected bound.
- Independent bridge and Python regressions passed; installation and live activation remain separate checks.
- Full Manager Chat, shared all-seat context policy, sandboxed OpenCode/Muse and Skills completion are not claimed.

## What's new in 0.12.20

- Editor workers use a dedicated write-only MCP connection, preserving Source Graph/prepare/apply session identity without granting worker launch or dashboard write permissions.
- Manager/dashboard closure preserves worker targets; repository switch and extension shutdown dispose their connection.
- Independent full extension and Python authorization checks passed; packaging, installation and a genuine live worker edit remain pending.
- Full Manager Chat, sandboxed OpenCode/Muse and Skills coverage are not claimed.

## What's new in 0.12.19

- Restoration candidate: Windows worker validation prefers repository-local temporary storage, and language-model workers continue on verified Source Graph progress without weakening stall safeguards.
- Zero-diff disposition fixtures support Git long paths and owned cleanup of read-only Git objects.
- Focused independent checks passed; full final Python qualification remains pending after a previous run reported 35 failures. Installation and live activation are not yet verified.
- Manager Chat completion, sandboxed OpenCode/Muse startup and Skills invocation coverage are not claimed.

## What's new in 0.12.18

- Manager Chat now has safe Markdown, live partial text, command/diff blocks, task links, a real usage footer, context hairline, transcript pagination and scroll control.
- Worker completion and Source Graph progress guards no longer confuse valid work with premature completion or discovery stalls.
- Pending model reroutes and skills evidence are wired to canonical authority; both dashboards expose separate unpriced main/subagent transcript usage.
- Declared worker contexts carry task-scoped Development Rules; dashboard cost headers distinguish unknown cost, partially priced totals and observed zero.
- Full Python and extension release gates passed; live activation and sandboxed OpenCode/Muse startup remain explicitly pending.

## What's new in 0.12.17

- Source Graph retries a briefly contended index recovery instead of failing the refresh.
- A review that became ready while the manager was busy is announced again instead of being lost.
- Manager Chat keeps its own conversation when you switch the model, shows a provider's real failure text, keeps long replies whole, and re-delivers a wake callback whose turn failed.
- An OpenCode manager seat starts in the manager repository.
- The semantic-edit coverage figure no longer counts files a rework attempt only inherited.

## What's new in 0.12.16

- Source Graph recovers an interrupted index without locking it: an open reader no longer makes recovery hang for half a minute with `database is locked`.
- A failed tool call from a VS Code language-model worker reports its real reason instead of "MCP unavailable".
- A language-model worker is stopped for malformed replies only when they come one after another.

## What's new in 0.12.15

- A card whose launch failed before the worker started can be retried on the same task instead of staying stuck.
- A worker's state and liveness stay visible while it reports a runtime notice.
- A validation replay that never ran a test is reported as a finalization failure, not as failed tests.
- Recovering a blocked rework keeps the rejected candidate and can still be routed to another worker.
- A failed promotion on Windows names the file, the operation and the cause, and a briefly locked file is retried.
- A worker is never started on an empty prompt.
- Groundwork for the admin-free Windows worker sandbox: repo-local slots and a restricted-token launch primitive. It is not active for workers yet.

## What's new in 0.12.14

- A review now shows how many tests passed, failed and were skipped, and the first failure, instead of only an exit code.
- Reworking a card whose files changed on main in the meantime launches again instead of stopping on a hash check.
- Stopping a worker on Windows ends its whole process tree and never reports a still-running process as stopped.
- Running the test suite no longer overwrites the built release package.
- A Codex manager that switched to another repository can switch back instead of stopping on a route ownership conflict.

## What's new in 0.12.13

- A manager can dismiss a low or medium reviewer finding with counter-evidence, on the record, instead of re-running the whole card.
- Two refusals now say why: a blocked recovery names the failing check, and a refused finding disposition names what is missing.
- A test command that ran zero tests no longer counts as passing evidence.
- Source Graph recovers from a damaged search index and keeps the real build error after a lost startup race.
- Workers on non-UTF-8 Windows locales read git output correctly, and fresh worktrees no longer show seeded files as modified.

## What's new in 0.12.12

- The NeedFix header shows the real open and stored counts instead of a constant 200.
- A launch that failed without changing anything can be recovered instead of blocking its card.

## What's new in 0.12.11

- A rejected review can demand new evidence: `validation_amendment` appends the required commands to the card's validation, so the next attempt must run them.

## What's new in 0.12.10

- Concurrent reviewer launches on Windows share one AppContainer profile instead of racing each other into a failed launch.
- Worker-sandbox test runs count as real coverage again: `tmp_path` tests run instead of skipping silently, and a reviewer that could not inspect is replaced rather than reused.
- Converted NeedFix rows are attributed to the card that introduced the defect, and Context MCP / KB writes no longer fail on read-only seats or legacy timestamp schemas.

## What's new in 0.12.9

- C/C++ code tasks launch with their project headers: angle-bracket includes resolve against the declared include roots.
- The Claude haiku route launches instead of failing with `model_not_found`.
- Rejecting a review stops its now-useless reviewers, and OpenCode workers lose the file-writing builtins they must not use.

## What's new in 0.12.8

- Rework and ranged `read_first` launches no longer fail on an emptied worktree or a `path:N-M` context suffix.
- Reviewer routing skips sandbox-dead adapters, and a running reviewer is never launched twice for one lens.
- Path scope checks fail closed on `.//`, leading-`/` and out-of-scope residual paths.

## What's new in 0.12.7

- SDLC stage proofs no longer stall: accepted cards that were archived later, and cards whose files moved on after accept, still reach build, test and deploy.
- Escaped-defect attribution counts only real, converted defects, so the quality metric is honest.

## What's new in 0.12.6

- Every card walks all six SDLC stages on its own, through deploy and maintain, each proven from receipts.
- Rework is safer: a reworked card keeps the previous attempt's changes on top of newer code, and a blocked rework can be recovered.
- Fewer stuck launches: a missing adapter is derived, stale reconciler locks hand over, and reviewer reuse matches the sealed attempt.

## What's new in 0.12.5

- The SDLC loop measures itself: quality drifts beyond 2σ/3σ file a NeedFix automatically, and escaped defects are traced to the card that introduced them.
- Every release leaves a receipt (commit, VSIX digest, rollback target, installed confirmation).
- Sandboxed workers can run their validation while they work, instead of finding failures only after they exit.

## What's new in 0.12.4

- Cleanup is automatic: after every task decision AIWorkHub removes the worktrees, logs, deltas and records nobody needs any more, and files a NeedFix if something cannot be removed.
- Worker effort follows the card's declared difficulty, so simple cards stop paying for maximum reasoning.
- Reviews are cheaper: one bounded read pass, and no duplicate reviewer for the same lens.

## What's new in 0.12.3

- Reworked cards are reviewed only on what changed since the last round, so a small fix no longer pays for a full review.
- Reviewers see the manager's rework instructions sealed into their own packet.

## What's new in 0.12.2

- AI Memory rebuilds a lost search index by itself, so worker cards stop failing on `fts_unavailable`.
- A forgotten Manager Console test session no longer claims to be the manager of every chat.
- Semantic edits land on the right lines in files with unusual line separators.
- Reviewers are no longer charged for supervisor faults, and directory diffs catch byte-only edits.

## What's new in 0.12.1

- Cards can be created and validated for every language Source Graph indexes, not only Python.
- Source Graph can compare two directory trees, down to changed hunks and changed symbols.
- Reviewer findings are no longer lost when they contain Unicode line separators.
- Each review lens runs one live reviewer, and findings point at indexed symbols.

## What's new in 0.12.0

- Sandboxed validation can create git repositories again, so launcher test suites run inside the sandbox.

## What's new in 0.11.99

- Fixes that land on main close their NeedFix automatically through a `Resolves:` commit trailer, so the NeedFix count follows the work.
- Card validation spends about 80 seconds less on the declared-invariants gate.
- Searching code for an exact quoted phrase finds it in the indexed source.

## What's new in 0.11.98

- Sandboxed Claude workers on Windows run shell commands through PowerShell, so they no longer stall on a shell that cannot start.
- Sandboxed validation parses code with a grammar copy kept inside the repository, with no C: drive access.

## What's new in 0.11.97

- A worker route that runs out of monthly credit is skipped automatically until it resets.
- Cards made from templates keep the repository's safety checks even when validation is overridden.

## What's new in 0.11.96

- Validation inside the Windows sandbox can run `git` again, so correct worker changes stop landing as validation failures.
- NeedFixes can be closed against a verified commit when their card was superseded.

## What's new in 0.11.95

- Rework relaunch and upgrade GC work again for workspaces created before the sandbox moved into the repository.
- Source Graph self-repairs a damaged full-text index; terminal failures keep their measured cause.

## What's new in 0.11.94

- Windows AppContainer worker sandboxes moved from `%TEMP%` into the repository; launches no longer touch C: permissions.

## What's new in 0.11.93

- Windows AppContainer workers no longer spend minutes to hours walking the user profile at launch and close.

## What's new in 0.11.92

- The route panel names the open Manager Chat session instead of a pending Codex thread.

## What's new in 0.11.91

- A callback reaches both the Codex or Claude thread and Manager Chat. An active manager session can own the task without a Codex thread.

## What's new in 0.11.90

- Thinking and tool calls appear during a manager turn. Reasoning depth is a control next to the model.

## What's new in 0.11.89

- Callback tasks can be created from the active Manager Chat session.

## What's new in 0.11.88

- Turning an OpenCode model on stays on. Settings no longer redraw that switch from an older snapshot.

## What's new in 0.11.87

- A chosen model uses its own CLI. The transcript stays empty until you open a saved session or send.

## What's new in 0.11.86

- Pick a model, not a provider. The session binds to the CLI that runs that model.

## What's new in 0.11.85

- Provider and model dropdowns keep the selection you click.

## What's new in 0.11.84

- Sessions are yours. Changing the model continues the same session instead of hiding or splitting the list by provider.

## What's new in 0.11.83

- The chat no longer lists dashboard tasks. Sessions follow the selected backend, and Delete removes one you do not want.

## What's new in 0.11.82

- Manager chat loads the last session, opens the first one on the selected model, and writes that session to Context Graph. Callbacks follow the active session.

## What's new in 0.11.81

- Dotted Python imports and exact type annotations now resolve. Stdlib names stay unresolved.

## What's new in 0.11.80

- MCP tool calls from the extension bind to their Python handlers. Member calls such as document.createElement do not.

## What's new in 0.11.79

- Source Graph health no longer stays silent when Python and JavaScript never call across the boundary.

## What's new in 0.11.78

- AppContainer workers use PowerShell. Source Graph overlays no longer die on a cross-device hardlink.

## What's new in 0.11.77

- Editor-hosted workers can stage a one-line edit with a string line number. The bridge no longer rejects that shape as `range_invalid`.

## What's new in 0.11.76

- Manager Chat stays beside the dashboard. Source Graph bodygrep can resume a
  truncated scan, and a too-long validation temp no longer blocks the nested
  LSP helper.

## What's new in 0.11.75

- VS Code LM workers stop a Source Graph loop that never edits, even when
  each query is different.

## What's new in 0.11.74

- VS Code LM workers fail closed on repeated line-1 edit pins and invalid JSON
  instead of looping until the turn limit.

## What's new in 0.11.73

- `node` and `node --test` validations run inside the Windows AppContainer
  instead of hanging on child-process pipes or failing on protected ancestors.

## What's new in 0.11.72

- Bundled Kilo/Grok workers receive a safe request-local XDG state directory.
- Windows AppContainer traversal grants stay below trusted user Temp and reject
  protected-root or pre-creation reparse-point escapes.

## What's new in 0.11.71

- Editor-hosted workers terminate repeated unchanged Source Graph discovery
  early, while legitimate query-boundary changes remain available.

## What's new in 0.11.70

- Native OpenCode 2 workers route Bun temporary files to their request-local
  AppContainer temp directory.
- The runtime module-size invariant passes at the existing threshold.

## What's new in 0.11.69

- Manager Chat starts automatically on first send using an allowed configured
  route.
- LSP-backed Source Graph and focused delta review are included.
- Windows workers provision Kilo's request-local XDG state path.
- Development Rules shows applicable-rule counts honestly.

## What's new in 0.11.68

- C/C++ cards launch regardless of the include layout.

## What's new in 0.11.67

- Manager panel: Codex sessions start and keep their conversation.

## What's new in 0.11.66

- Model lists follow the CLIs and show versions.

## What's new in 0.11.65

- Manager panel: the Claude session now holds the AIWorkHub manager tools.

## What's new in 0.11.64

- Manager panel: a model list that fills itself, and a loop that can write and
  launch in its own MCP child while the dashboard stays read-only.

## What's new in 0.11.63

- A Manager chat panel in the dashboard: pick a model, talk to the manager,
  and see task callbacks wake it by themselves.
- Compact manager tool summaries and Claude Code usage in the cost ledger.

## What's new in 0.11.62

- Windows: a card sent back for rework runs again. The worker prompt reaches
  the CLI through stdin instead of the length-capped command line.

## What's new in 0.11.61

- The manager seat has a host-side launch plan, so the manager CLI backend can
  run on Windows; workers stay inside AppContainer.
- Windows AppContainer launch failures name their Win32 cause and sizes, never
  their values.
- The validation lane skips host-privileged AppContainer tests by name instead
  of failing on them.
- Source Graph skips nested linked git worktrees.

## What's new in 0.11.60

- The provider-neutral core of the AIWorkHub manager agent loop: one manager
  session per repository, a bounded rehydration brief, one turn at a time and
  rotation through a handoff, so the managing model can be switched without
  losing state.
- A validation-only replay grant is honoured from any verified manager route,
  not only `codex`.
- Roadmap, NeedFix backlog, recipes, skills, KB and AI memory travel with a
  clone.

## What's new in 0.11.59

- Windows: bundled native `claude_cli` workers now run inside their
  AppContainer with their worker tools. The worker MCP server runs on the host,
  behind a per-request pipe. Worker launches get outbound internet only.
- Windows AppContainer validation runs pytest, ruff and a hardened host
  `git diff --check`.
- Dashboard: a compact header strip with status dots, quieter counters and
  readable light-theme contrast.
- Semantic edits cannot be redirected through a junction planted in the
  worktree.
- CMake `include/` headers resolve, and `cmake`/`ctest` are trusted.
  Blocked-card rework recovery works on Windows. Reviewer prewarm can no longer
  wedge the launch queue.
- Python in an admin-owned directory outside Program Files needs a one-time
  elevated `icacls "<python dir>" /grant "*S-1-15-2-1:(OI)(CI)(RX)" /T`. The
  launch error names it. Per-user, Program Files and Store installs need
  nothing.

## What's new in 0.11.58

- The bundled runtime's first four SDLC case stages are gated on server-proven
  evidence (NF-2026-00945): Plan, Design, Build and Test can be recorded
  `ready` only when the runtime proves them from the repository's own canonical
  receipts, a caller can no longer self-declare a verdict, and a `ready` receipt
  is re-proven on every read. Receipts recorded before this gate stay visible
  for audit but no longer count as proof.
- Deploy and Maintain remain explicit refusals that name each missing producer:
  the SDLC Deploy and Maintain gates do not yet consume canonical deploy and
  release receipts, outcome metrics or policy, so a case's six-stage cycle
  cannot report complete; `not_applicable` is refused until a canonical policy
  registry exists.
- The bundled runtime's isolated launch now puts an OpenCode worker's
  request-local `awh` MCP config into the worker's own environment on the Linux
  (Landlock, bubblewrap) and Windows AppContainer paths, and refuses the launch
  before any process spawns when the config contract is not met
  (NF-2026-00919). This is launch wiring covered by unit and integration tests
  only: live OpenCode/Muse worker qualification and Windows runtime
  qualification remain unmeasured.
- LSP index integration, the OpenCode manager callback, the full stage-gated
  Playbook lifecycle and reasoning-quality measurement are not part of this
  release and remain pending; no reasoning-quality improvement is claimed.

## What's new in 0.11.57

- The bundled runtime's reviewer and rework Source Graph overlays now pin the
  exact base index generation they were built against, so an ordinary
  canonical index publication no longer breaks an in-flight review and a
  replaced or mutated pin fails closed (NF-2026-00946).
- The bundled runtime lets a manager reroute a retained candidate after a
  zero-delta launch failure (for example a provider authentication failure)
  that blocked-rework recovery already returned to pending, authorized once by
  the canonical claim, launch-failure and recovery chain (NF-2026-00778).
- LSP index integration, OpenCode/Muse worker qualification, the full
  stage-gated Playbook lifecycle and reasoning-quality measurement are not part
  of this release and remain pending.

## What's new in 0.11.56

- The bundled runtime's explicit manager recovery of a blocked task can now
  recover a timed-out candidate whose worktree retention already collected,
  from the delta sealed when the attempt terminated. The delta is accepted only
  when it authenticates against the exact repository, task, request, claim epoch
  and hash-pinned changed paths; anything else fails closed and leaves the task
  unchanged (NF-2026-00594).
- LSP index integration, the OpenCode manager callback, provider-neutral
  Playbook completion, Muse worker qualification and portable `.aiworkhub` data
  are not part of this release and remain pending.

## What's new in 0.11.55

- The wave mini-roadmap popup now follows the bundled runtime's current-wave
  projection instead of ranking Roadmap rows itself: it shows the installed
  version and the wave's target separately, marks a passed target overdue, and
  never checks a goal the runtime did not or counts an archived or stale task
  as done. Ambiguous, truncated or malformed evidence shows a typed UNKNOWN
  reason.
- Task creation, including from a template, accepts an optional exact
  `wave_goal_binding` that replaces a named predecessor as one wave goal's
  current task. It is applied once and never inferred from titles or prose.
- The bundled runtime's reconciler completes a wave only when every acceptance
  criterion maps to a goal whose exact tasks are all canonically accepted with
  verifier receipts; pending or unresolved evidence leaves it open, and no
  version bump can close it.
- Worker validation sandboxes in the bundled runtime seed the tracked repository
  assets a declared test locates by a literal path (NF-2026-00551).
- Inferred successor progression, the full stage-gated Playbook, LSP index
  integration and Muse/OpenCode worker qualification remain incomplete.

## What's new in 0.11.54

- The bundled Source Graph runtime treats authenticated concurrent index builds
  as healthy standby instead of blocking code tasks on a fresh index.
- Automatic mini-roadmap progression and the full Playbook/LSP work remain in
  progress.

## What's new in 0.11.53

- The bundled semantic reviewer requests hash-matched candidate overlays for
  omitted hunks and still fails closed on missing or stale evidence.
- Bundled task creation checks required outputs; terminal events distinguish
  validation failures from provider timeouts and bound dashboard snapshots.
- Worker-attempt reasoning/context receipts are durable, not proof of
  provider-internal effort or improved quality.
- Full Playbook stage gates, LSP index integration and Muse/OpenCode worker
  qualification remain in progress.

## What's new in 0.11.52

- The wave mini-roadmap now shows the current wave's goals as a live checklist
  joined to each goal's task states.
- The bundled runtime bounds semantic review to a candidate's exact changed
  segments and fails closed on missing or stale changed-segment evidence.
- Source Graph ships a bounded LSP transport with fail-closed definition
  classification over a private workspace. LSP index integration is not
  included in this release.
- Blocked-rework recovery lets a strictly later terminal failure supersede a
  stale retained predecessor (NF-2026-00515).
- The full stage-gated Playbook, reasoning matched to an accepted outcome, and
  Muse/OpenCode worker qualification are still incomplete.

## What's new in 0.11.51

- The bundled runtime exposes repository-bound SDLC case receipts and exact
  task links. Full Playbook stage gates are still under development.
- VS Code LM records the exact reasoning option handed to `sendRequest` and
  the model-reported context capacity, while leaving provider-internal effort
  unknown. Accepted-task outcome metrics now report complete-history coverage.
- OpenCode uses the short `awh` MCP alias; worker recovery and required-output
  handling are hardened. Semantic Review's delta scope and LSP resolution are
  not claimed as complete in this intermediate release.

## What's new in 0.11.50

- The identity strip has a wave mini-roadmap info popup for the current
  in-progress Roadmap wave.
- VS Code LM requests apply reasoning effort only when the model
  declares a selectable control that honors the canonical profile;
  context capacity is recorded and never used to pad the prompt. CLI
  workers in the bundled runtime follow the same APPLIED-only rule.
- Native reviewer retained streams also compact thinking deltas.
- Sparse-worktree VS Code test-asset seeding and the sandbox-safe
  OpenCode config check are repository-only test/manager work, not
  VSIX features.

## What's new in 0.11.49
- This intermediate release combines the staged Windows and OpenCode fixes
  below; the 0.11.45-0.11.48 headings were development notes, not separate
  published releases.
- OpenCode's global AIWorkHub MCP server is registered as `awh` so models with
  a short MCP-name limit can use it without changing the server's identity.
- The bundled runtime includes outcome-linked NeedFix metrics; the source
  repository release includes the resealed accepted-task evaluation corpus
  (the corpus is not packaged in the VSIX).

## What's new in 0.11.48

- OpenCode's model list in Settings no longer crowds out newly-discovered
  models behind a heavily-toggled provider's history; today's full catalog
  fits without truncation.

## What's new in 0.11.47

- OpenCode's model list in Settings now stays current regardless of which
  panel you opened first.

## What's new in 0.11.46

- Windows: worker launches no longer fail with an unexplained authority-key
  error after upgrading.
- Windows: OpenCode now appears in model settings when it's installed.

## What's new in 0.11.45

- Windows: Claude Code can now hold the manager seat. The venv launcher's
  redirector process no longer blocks identity verification.
- Windows: native-CLI sandboxing (AppContainer confinement) works on capable
  hosts again, instead of reporting unavailable everywhere.

## What's new in 0.11.44

- Windows: creating a task no longer stalls. A child process that inherited the
  MCP server's request pipe hung before running its own first instruction, so
  every `git` the coordinator ran burned its whole timeout — task creation now
  answers in 0.09 s instead of 120 s.
- Windows: installed tools are measured again, so a card requiring
  `node>=20.0.0` and `ruff>=0.12` is no longer refused as unwinnable on a
  machine that has them.
- Windows: the authority key no longer follows a symlink, and its owner and ACL
  are checked against the open file's security descriptor.
- Windows: the reconciler heartbeat is readable again, reviewer cards no longer
  stay stuck in `processing`, and connecting a repository works again.

## What's new in 0.11.43

- The manager can now make small, hash-bound range edits directly through the
  MCP semantic-edit tools workers already use.
- A new read-only `aiworkhub_dashboard_skills` MCP surface reports measured
  skill-selection coverage — how many recorded receipts actually selected and
  injected a skill, and the streak of recent receipts that injected nothing —
  reporting "not measured" with a reason instead of a healthy-looking zero
  when the store can't be read.
- A new read-only attempt-trajectory export composes one request's audit
  history, process lifecycle, artifacts and usage into a single canonical
  JSON document, with unmeasured fields reported as `UNKNOWN` rather than
  guessed.
- Source Graph's `calls` mode now resolves a query to the one symbol that
  actually defines the queried name before returning call edges, instead of
  also matching every import, decorator or annotation that merely mentions
  it.
- The Windows sandbox report now names the exact measured cause native CLI
  execution was refused instead of one fixed blocker code. This does not
  change, and does not claim, which Windows routes are launchable.
- Oversized Source Graph analytic-mode results from the worker AI-tools MCP
  Source Graph route are now spilled to a repository-scoped store before
  being trimmed for display, so the full original stays retrievable instead
  of being discarded the moment it is truncated. The store has no eviction,
  TTL or size cap yet.
- A duplicate launch request for a task already attached to a live worker now
  returns the existing claim instead of recording a new blocked episode that
  could overwrite the original worker's ownership.

- The Models view keeps every provider visible and keeps explicitly configured
  routes reachable when a bounded catalog read comes back short. Rows are chosen
  per provider only after every provider has been seen, routes named in
  `.aiworkhub/config/models.json` are reserved under both their written and
  canonical identities, and pins that do not fit past the hard ceiling are
  counted as refused instead of disappearing.

- Model counts no longer present an upstream-truncated list as an exact total.
  Each row states the bound that produced it — declared but not discovered,
  configured with its origin unknown past the host bound, or configured past the
  source bound — and the view names the host that cut the tail instead of
  blaming the editor cap for OpenCode rows.

- Compact dashboard counters keep four significant digits, so 1000 reads as 1k
  and 1001 as 1.001k rather than collapsing to the same label, 999999999 reads
  as 1B, and the decimal separator follows your locale.

- Windows native-CLI workers now run inside the repo-scoped AppContainer
  profile and its kill-on-close Job Object when the platform, the host's
  AppContainer APIs and the launch path all confirm it, and the confinement
  report names the boundary actually in force instead of a fixed answer. The
  wiring is proved by fake-Windows behaviour tests over the real launch path;
  a live Windows canary has not run, so no live-Windows evidence is claimed.

- A Windows extension host resuming from idle no longer loses repository
  discovery to one transient handle, sharing or lock fault. The manifest read
  retries exactly once, only for an authenticated transient cause, on a
  brand-new descriptor that repeats every symlink, regular-file and identity
  check. A missing, malformed, foreign or otherwise invalid manifest still
  fails closed immediately.

- Rejected validation-only candidates now lose their one-episode replay grant,
  ensuring the next rework claim invokes a worker instead of repeating stale
  validation forever.
- Reviewer cleanup is parent-scoped, so rejecting one candidate cannot finalize
  or cancel unrelated review work.

- Review recovery now preserves fair cursor progress when its first action is
  deferred, allowing later ready reviewer chains to run in the same bounded
  reconciler pass.

- Reconciler review recovery now advances a bounded batch of durable actions
  per scan and continues past deferred chains. A busy review queue can no
  longer strand newly seeded reviewers behind one action per multi-minute pass.

- Task MCP writes now share a serialized writer boundary and reusable lock
  descriptor, reducing SQLite contention and lock churn.

- Toolchain receipt creation and validation now use one bounded executable
  fingerprint contract, eliminating false identity drift for binaries larger
  than 1 MiB during provider-free replay.

- Durable toolchain snapshots are executable-identity checked before reuse;
  stale cache facts are re-derived instead of blocking retained-candidate
  validation before its first declared command.

- Provider-free validation replay now preserves non-empty `read_first`
  requirements in its authenticated toolchain identity.
- Repeated validation-only replay can inherit the authenticated worker MCP gate
  through a mechanically failed replay without launching the provider again.
- Validation-only replay now accepts an exact authenticated retained delta
  even when the canonical parent advanced after the original attempt.
- Validation-only replay now carries the complete HMAC-bound task identity,
  allowing retained candidates to rerun gates without false receipt mismatch.
- Sealed `review_ready` candidates whose automatic reviewer child was never
  created are recovered by reconciliation without manager-launched reviewers.
- Mechanical review parks, retained-delta reroutes and pending launch failures
  preserve their authoritative lifecycle across retries.
- Read-only analysis and research no longer consume mutation-only validation or
  reviewer work.
- OpenCode workers receive isolated authentication, classic Snap launch support
  under Landlock, and explicit capacity-refusal evidence.
- VS Code LM finalization keeps valid partial finals while contradictory or
  unauthenticated terminal evidence remains rejected.

Automatic quality-review launch moves worker completion through a
system-owned correctness, security, and code-quality chain without
prematurely waking the manager. After every required lens passes, one
authenticated aggregate binds the candidate and reviewer evidence and emits
exactly one manager callback. Review automation never accepts or rejects the
implementation target; the verified manager receives only the completed
decision packet. Legacy review receipts remain compatible without blocking
later chains.

Detailed older history stays in the
[changelog](https://github.com/shrec/AIWorkHub/blob/main/vscode-extension/CHANGELOG.md).

## Architecture at a glance

<div align="center">
  <img src="https://raw.githubusercontent.com/shrec/AIWorkHub/main/docs/assets/aiworkhub-block-diagram.png" alt="AIWorkHub system architecture block diagram" width="100%">
  <br>
  <em>The complete AIWorkHub control plane, execution, evidence and improvement loop.</em>
</div>

<div align="center">
  <img src="https://raw.githubusercontent.com/shrec/AIWorkHub/main/docs/assets/aiworkhub-source-graph-architecture.png" alt="AIWorkHub Source Graph architecture" width="100%">
  <br>
  <em>Incremental Source Graph refresh, index, query, semantic-edit and review-overlay paths.</em>
</div>

<div align="center">
  <img src="https://raw.githubusercontent.com/shrec/AIWorkHub/main/docs/assets/demo/aiworkhub-task-review-loop.gif" alt="AIWorkHub task, worker, evidence and review loop" width="100%">
  <br>
  <em>Create a bounded task, launch a model worker, inspect its evidence and accept or rework it.</em>
</div>

## Highlights

- Plan and inspect dependency-aware AI tasks from one operational dashboard.
- Delegate to supported local model adapters and track real terminal outcomes.
- Replace repeated raw-source discovery with a repository Source Graph covering
  exactly 34 language/file families.
- Send focused code fragments through staged semantic edits and let the local
  bridge assemble the hash-bound final envelope without model-side full-file
  regeneration.
- Use exactly 37 currently exposed bounded Source Graph query modes for
  symbols, calls, tests, impact, complexity, ownership, hotspots, gaps and
  task-shaped context bundles.
- Preserve continuity through Session Manager, AI Memory and KB.
- Review diffs, tests, logs, artifacts, approval history and deterministic
  Quality Evidence before acceptance.
- Run a changed-file Known Bug Scanner across C/C++/CUDA, Python,
  JavaScript/TypeScript, Go, Java/Kotlin and PHP without treating heuristic
  warnings as proven failures.
- Measure whether workers used Source Graph throughout the task through
  authenticated tool-use receipts and continuous-use telemetry.
- Keep repositories isolated in separate `.aiworkhub/` authorities.
- Run on Linux (the canonical development and validation host), on WSL,
  Remote-SSH and macOS as qualified client paths, and on native Windows only
  at its measured coverage.

## Operational dashboard

The retained dashboard combines the task DAG, live worker output, Review
Inbox, callback health, model readiness, tool-use statistics, storage
retention, Source Graph coverage and bounded viewers for logs, sessions,
AI Memory and KB. Settings remain repository-local under `.aiworkhub/`, so a
multi-window installation does not share task or context authority between
repositories.

<div align="center">
  <img src="https://raw.githubusercontent.com/shrec/AIWorkHub/main/docs/assets/screenshots/aiworkhub-self-hosted-dashboard.png" alt="AIWorkHub repository dashboard" width="100%">
  <br>
  <em>Tasks, callback health, source coverage, context stores, preflight and evidence in one retained editor tab.</em>
</div>

## Get started

1. Install from the Marketplace (or install a release VSIX) and open a Git
   repository in VS Code.
2. Run **AIWorkHub: Open Dashboard**.
3. Select the repository when using a multi-root workspace.
4. Choose **Initialize AIWorkHub** on first use.
5. Open a new Codex, Claude or MCP-capable chat after registration so the new
   runtime tools are discovered by that chat process.

Initialization is explicit and idempotent. It creates repository-local state
only under `.aiworkhub/` and starts the first Source Graph index in the
background.

For Claude Code, initialization also maintains the repository-local
`.mcp.json` server registration and the bounded AIWorkHub block in `CLAUDE.md`.
Open a **new** Claude chat after initialization or an AIWorkHub upgrade. That
direct chat is instructed to bootstrap as the manager, call manager Source
Graph before broad `Read`/`Grep`/`Glob` discovery, and re-query the graph when
its implementation or validation boundary changes. AIWorkHub-launched task
processes use the separate worker tool surface.

## Run your first task

AIWorkHub is designed for a manager chat that delegates bounded work instead
of letting several models edit one checkout without coordination.

Start a new chat after initialization or upgrade and paste:

```text
Use AIWorkHub as manager for the currently bound repository. Call
aiworkhub_manager_bootstrap first; verify repository identity, manager route,
callback, Source Graph and preflight. Do not edit or launch yet. Report what is
ready and what is degraded.
```

Then describe the desired outcome normally. Ask the manager to create bounded
cards and launch only independent, dependency-ready, non-colliding cards in
parallel. The MCP server also presents this lifecycle as a mandatory contract:
creating a task leaves it `pending`; exact claim plus launch establishes
`processing`; workers stop at `review_ready`; callbacks wake the current
verified manager; and only that manager accepts or rejects verified evidence.

1. Check the dashboard header. Repository, MCP, Source Graph and callback
   state should be ready; **Preflight** explains any unavailable optional
   model adapters.
2. Tell the manager what outcome you want. The manager creates a task card
   with an exact objective, acceptance criteria, allowed writes, validation
   commands and dependencies.
3. The manager selects a ready adapter/model and launches the exact card.
   Workers receive repository-scoped Source Graph, Session, Memory and KB
   context and work in an isolated task workspace.
4. Follow **Live Output** or continue other work. Terminal outcomes are durable
   and the originating manager receives a callback when review is required.
5. Open the task in **Review**. Inspect the bounded diff, tests, logs,
   artifacts, tool-use receipts and independent reviewer evidence.
6. **Accept** promotes the verified change and finalizes the task. **Reject**
   records exact feedback and creates a bounded residual rather than silently
   discarding the previous evidence.

Dependency cards remain pending until their prerequisites finish. Collision
checks prevent two active workers from owning overlapping write paths.

See the [complete first-run and manager manual](https://github.com/shrec/AIWorkHub/blob/main/docs/GETTING_STARTED.md)
for copy/paste planning/review prompts, Remote-SSH behavior and recovery after
an interrupted write acknowledgement.

## Models and authentication

AIWorkHub does not proxy credentials or require an AIWorkHub account. It uses
models already authenticated in the corresponding editor or CLI. The table
lists observed runner families; Preflight reports which routes are actually
ready in this window. An optional adapter being unavailable does not block
otherwise ready models.

| Runner | Typical adapter | Requirement |
| --- | --- | --- |
| Codex | Codex CLI or VS Code Language Model | Existing Codex login or one-time VS Code consent |
| Claude | Claude Code CLI or VS Code Language Model | Existing Claude subscription login or one-time VS Code consent |
| Copilot-hosted models | VS Code Language Model | GitHub sign-in and one-time model consent |
| DeepSeek | VS Code Language Model or Copilot CLI fallback | Provider visible in VS Code; fallback uses its own stored credential |
| GLM 5.3 | VS Code Language Model or Copilot CLI fallback | Provider visible in VS Code; fallback uses its own stored credential |
| Grok | Observed Kilo/Grok worker route | Existing provider login; Preflight reports whether that route is ready |

## Source Graph and context

Source Graph is an incrementally refreshed structural repository index, not a
remote Sourcegraph service. It covers exactly 34 language/file families and
exposes exactly 37 currently exposed bounded Source Graph query modes.
Managers and workers start with low-token `focus` and `slice` queries, then
use calls, trace, impact, test mapping or typed bundles only when the task
needs them. Operations telemetry shows which modes were requested and
executed, returned evidence, workflow stage, latency, generation and
inter-call gaps.

Session Manager stores current state and handoffs; AI Memory stores durable
lessons; KB stores curated project facts; the optional Manager Context Graph
preserves bounded manager transcript evidence. All are repository-local and
have bounded viewers in the dashboard.

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

## If the dashboard is not ready

- **Connecting:** use **AIWorkHub: Restart MCP Connection** once and inspect
  the dashboard's last-log row. The extension restarts only its own child.
- **A model is unavailable:** open Preflight, confirm the provider is installed
  and grant the one-time VS Code model consent when prompted.
- **Source Graph is empty:** initialize the repository, enable the required
  language family in Settings and run a refresh.
- **A chat cannot see tools:** open a new chat after installation or upgrade so
  that client performs MCP discovery against the current runtime.
- **Windows upgraded from an old build:** activation automatically migrates
  legacy source/version `PYTHONPATH` registrations to a host-stable packaged
  runtime; no manual `config.toml` edit is required.

## Trust and privacy

- Local stdio transport; no AIWorkHub network listener.
- Read-only and launch-disabled by default.
- Repository-specific state, route identity and audit trail.
- No AIWorkHub telemetry upload of prompts, source, credentials or memories.
- Explicit manager authority for context writes and task acceptance.

Read the full [Getting Started guide](https://github.com/shrec/AIWorkHub/blob/main/docs/GETTING_STARTED.md),
[Architecture](https://github.com/shrec/AIWorkHub/blob/main/docs/ARCHITECTURE.md),
[Source Graph guide](https://github.com/shrec/AIWorkHub/blob/main/docs/SOURCE_GRAPH.md),
[Manager Context Graph guide](https://github.com/shrec/AIWorkHub/blob/main/docs/CONTEXT_GRAPH.md),
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
