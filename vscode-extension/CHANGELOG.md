# AIWorkHub for VS Code — Changelog

## 0.12.3 — 2026-09-28

### Changed

- A rework round is reviewed on the hunks that changed since the predecessor round: the candidate carries a sealed rework delta, and a lens that already judged the predecessor reviews only those hunks instead of the whole candidate. An unreadable or partial delta falls back to the full review surface with a recorded reason (NF-2026-01093).

### Fixed

- The manager's rework amendment is sealed into every reviewer lens packet, so the reviewer judges the candidate against the amended contract instead of the original card (NF-2026-01086).

## 0.12.2 — 2026-09-28

### Fixed

- Manager AI Memory search and writes repair a missing `memories_fts` index in place without losing rows, so a legacy or half-migrated store no longer fails the worker's mandatory AI Memory call with `fts_unavailable` (NF-2026-01083).
- A stale Manager Console session no longer holds the manager seat: bootstrap reports the calling chat's own route, task creation stops stamping the expired session as callback origin, and wake never starts on it (NF-2026-01078).
- Semantic edit numbers lines on `\n` only, so a range from Source Graph, git or an editor selects the same text in files holding bare CR, U+0085 or U+2028; a junction path component is refused like a symlink (NF-2026-01077).
- A reviewer spends a submit attempt only on its own rejected submission, never on a supervisor persistence fault (NF-2026-01074).
- Source Graph directory diff reports edits that differ only in invalid UTF-8 bytes as binary and names the correct side of a skipped file (NF-2026-01079, NF-2026-01085).
- The runtime symlink escape test skips only when the symlink privilege is missing, so the AppContainer validation lane no longer fails it (NF-2026-01071).

## 0.12.1 — 2026-09-28

### Added

- Source Graph directory diff core: compares two directory trees, classifies each file as added, removed, modified or renamed, and emits bounded unified-diff hunks plus per-symbol body changes (RM-2026-00071).
- A language-neutral `custom_validation` card template and host-toolchain trust for every Source Graph language family, so cards can be created and validated in all of them (NF-2026-01072 parts A and C).
- Reviewer ingest resolves findings through the index-proven symbol resolver; overbuild findings bind to indexed symbols and the reviewer prompt covers every lens item in one pass (NF-2026-01065, NF-2026-01057 part 1).

### Fixed

- Reviewer findings that contain U+2028, U+2029 or U+0085 are durable: the audit ledger splits on the line-feed character only, and rework overlays count lines the way the AST does (NF-2026-01076).
- One live reviewer per lens, with liveness taken from the launcher's per-event rule (NF-2026-01058, NF-2026-01069).
- Rework sealing reports `RuntimeTempError` as a recovery error, never a raw temp failure (NF-2026-01070).
- Validation records carry a bounded candidate-authority reference (NF-2026-01060 part A).
- Relinking a NeedFix with a verified integrated commit resolves it (NF-2026-01062).
- The GLM bridge over VS Code LM orders no-progress events correctly and rejects oversized tool input cleanly.
- The extension webview suite passes again, and `*.css` is pinned to LF so fresh worktrees match the suite's markers.
- AppContainer-lane symlink and launcher tests skip only on the measured missing capability (NF-2026-01071).

## 0.12.0 — 2026-09-27

### Fixed

- The Windows AppContainer git helper temp directory no longer uses 8.3-shaped path components, so in-container `git init` stops failing with rc=128 on the traverse-only sandbox root (NF-2026-01053).

## 0.11.99 — 2026-09-27

### Fixed

- Reading NeedFix reconciles explicit `Resolves: NF-YYYY-NNNNN` trailers on HEAD-reachable commits into verified resolution, so hand-integrated fixes close without manual transitions; a git timeout falls back to a bounded newest window and a held database write keeps the watermark for the next read (NF-2026-01048).
- Linking a NeedFix to an existing task with a verified integrated commit resolves it and reports `resolved: true` on the first link; active NeedFix state is derived from the linked card (NF-2026-01048, NF-2026-01049).
- Source Graph `bodygrep` searches the inner text of a quoted phrase instead of falling back to a token scan (NF-2026-01051).

### Performance

- The declared-invariants gate parses each module once and shares one scan per check, cutting a card validation from about 111 s to about 33 s (NF-2026-01052).

## 0.11.98 — 2026-09-27

### Fixed

- Contained Claude workers on Windows get the PowerShell tool instead of a Git Bash that cannot start inside the AppContainer; every Bash deny has a PowerShell mirror and recursive PowerShell discovery stays denied (NF-2026-01043, NF-2026-01046).
- Contained validation reads a host-owned tree-sitter grammar mirror under `.aiworkhub/runtime/tree-sitter-cache` instead of the per-user cache on C:, with a read-only grant scoped to that mirror (NF-2026-01042).

## 0.11.97 — 2026-09-27

### Fixed

- A VS Code LM credit-limit refusal opens that route's circuit until its reset, so unpinned launches stop re-picking an exhausted route (NF-2026-01036).
- Accept review's seam guard is green again: `_accept_manager_identity` is declared as an accept-review local name.
- A template card created with a validation override keeps the repository package gate (NF-2026-01041).
- Provider-output failure classifiers move out of `process_launcher` into their own module; the module size ratchet is back to 14148 (NF-2026-01041).
- Test suites follow their contracts: AppContainer sandbox-root grants (NF-2026-01039), the paged review packet, the codex stdin prompt, and LSP symlink tests that skip only when the symlink privilege is missing (NF-2026-01042).

## 0.11.96 — 2026-09-27

### Fixed

- The Windows AppContainer validation lane keeps `git` on `PATH`; only the interactive agent lane drops Git Bash, so candidate tests that spawn `git` stop failing in-container (NF-2026-01037).
- A NeedFix can link a superseded card when its fix is a commit verified on `HEAD` (`integrated_commit`), closing NeedFixes whose fix was integrated by hand (NF-2026-01030).
- Accept evidence `verified_by` names the resolved manager instead of a hardcoded provider (NF-2026-01038).
- Sparse worker checkouts follow `importlib` dynamic imports and module-file siblings (NF-2026-01024).
- Supersede/archive receipts are bounded, the quality-review packet is paged under the host inline limit, semantic edit refuses drive-qualified and UNC paths, and policy files are pinned to LF so autocrlf worktrees stay byte-exact (NF-2026-01028/01029/01033/01034).

## 0.11.95 — 2026-09-27

### Fixed

- Upgrade garbage collection accepts worker workspaces left under the pre-0.11.94 repository-namespaced `%TEMP%` root, so rework relaunches and the legacy drain are no longer refused with `gc_workspace_shape_mismatch`.
- Source Graph repairs a damaged full-text index in place instead of staying stale.
- Terminal failures keep their measured launcher and sandbox classes instead of collapsing into `runtime_error`.
- OpenCode configuration, TOML quoting and protocol line coercion each have one owner again, clearing the copied-helper drift on main.

## 0.11.94 — 2026-09-27

### Fixed

- Windows AppContainer worker sandboxes live inside the repository under `.aiworkhub/runtime/worktrees` and are reached through a per-logon drive letter; a launch no longer writes permissions on the user profile or `%TEMP%`.

## 0.11.93 — 2026-09-27

### Fixed

- Windows AppContainer workers start and finish without walking the whole user profile: parent-folder traverse permissions are written to one folder only.

## 0.11.92 — 2026-09-26

### Fixed

- Manager route details show the active Manager Chat session, model and backend.

## 0.11.91 — 2026-09-26

### Fixed

- Callbacks are sent to both the existing Codex or Claude thread and the active Manager Chat session.

## 0.11.90 — 2026-09-26

### Added

- Manager chat shows thinking and tool calls during the turn. A Reasoning depth control sets Claude and Codex effort.

## 0.11.89 — 2026-09-26

### Fixed

- Manager Chat can create a callback task against its active session without a Codex thread UUID.

## 0.11.88 — 2026-09-26

### Fixed

- An OpenCode model switch stays on. A stale settings refresh no longer redraws the toggles.

## 0.11.87 — 2026-09-26

### Fixed

- Choosing a model sends that model's CLI, not the hidden default. The chat stays empty until you continue a saved session or send.

## 0.11.86 — 2026-09-26

### Changed

- The provider combo is gone. Pick a model; that session binds to the CLI that runs it.

## 0.11.85 — 2026-09-26

### Fixed

- The provider and model combos keep the click. A status refresh no longer snaps them back to the session's last route.

## 0.11.84 — 2026-09-26

### Changed

- A manager session is the owner's conversation. The session list is not split by provider, and the selected model rebinds that same session.

## 0.11.83 — 2026-09-26

### Changed

- Manager chat no longer repeats the dashboard task list. The session list follows the selected backend, and Delete removes a session you do not want to keep.

## 0.11.82 — 2026-09-26

### Added

- Manager chat loads the last session, opens the first session on the selected model, and renames the active session. Callbacks follow that session, and its turns are written to Context Graph.

## 0.11.81 — 2026-09-26

### Fixed

- Source Graph binds dotted Python imports and same-file or imported annotations. Bare stdlib names stay unbound.

## 0.11.80 — 2026-09-26

### Fixed

- Source Graph no longer treats document.createElement as a local helper, and it binds an exact MCP tool call to its Python handler.

## 0.11.79 — 2026-09-26

### Fixed

- Source Graph health flags a missing Python/JavaScript call boundary instead of staying silent.

## 0.11.78 — 2026-09-26

### Fixed

- Source Graph overlays stay open when a cross-device hardlink fails.
- AppContainer workers use PowerShell instead of Git bash.

## 0.11.77 — 2026-09-26

### Fixed

- VS Code LM staging accepts string line numbers and `action`/`path` aliases. A one-line edit is no longer rejected as `range_invalid` before the worker sees it.

## 0.11.76 — 2026-09-26

### Fixed

- Manager Chat is a persistent sidebar beside the dashboard.
- Semantic edit accepts a one-line range and a string line number.
- Windows validation-only replay uses the configured temp worktree.
- AppContainer launch no longer writes a DACL on C:\Users.
- Recovered cards can be rerouted without a review-rejection receipt.
- Truncated Source Graph bodygrep resumes from the returned cursor.
- Impact no longer drops a symbol's recorded callers when the file sample is full.
- Python crashes findings with zero precision stay advisory.
- A too-long validation temp is rebound so the nested LSP helper cwd can start.

## 0.11.75 — 2026-09-26

### Fixed

- VS Code LM workers stop after six Source Graph queries without an edit,
  instead of varying the query until the turn limit.

## 0.11.74 — 2026-09-25

### Fixed

- VS Code LM workers fail closed on repeated line-1 edit pins and invalid JSON
  instead of looping until the agent turn limit. Worker semantic edit stays on
  the worker editor.

## 0.11.73 — 2026-09-25

### Fixed

- `node` and `node --test` validations run inside the Windows AppContainer
  instead of hanging on child-process pipes or failing on protected ancestors.

## 0.11.72 — 2026-09-25

### Fixed

- Bundled Kilo/Grok workers receive a safe request-local XDG state directory.
- Windows AppContainer traversal grants stop at trusted user Temp and reject
  protected-root or pre-creation reparse-point escapes.

## 0.11.71 — 2026-09-24

### Fixed

- Editor-hosted workers stop repeated unchanged Source Graph loops before the
  broad agent-turn limit while preserving legitimate boundary changes.

## 0.11.70 — 2026-09-24

### Fixed

- Native OpenCode 2 workers route Bun temporary files to the request-local
  AppContainer temp directory instead of the package-private temp fallback.
- The Python runtime satisfies the existing module-size ratchet again.

## 0.11.69 — 2026-09-24

### Added

- Manager Chat chooses an allowed configured route and starts on first send.
- The bundled runtime includes the accepted LSP-backed Source Graph and focused
  delta-review paths.

### Fixed

- Windows AppContainer launch provisions Kilo's request-local XDG state leaf.
- Development Rules labels context-matching rules as applicable, not resolved.

## 0.11.68 — 2026-09-23

### Fixed

- Bundled runtime: C/C++ cards are no longer refused over quoted includes the
  seeding preflight cannot place.

## 0.11.67 — 2026-09-23

### Fixed

- Manager panel: Codex sessions start with the discovered models and keep
  their conversation across messages.

## 0.11.66 — 2026-09-23

### Changed

- Manager panel and model catalog: Codex and Claude model lists are
  discovered from the CLIs themselves and show model versions.

## 0.11.65 — 2026-09-23

### Fixed

- Manager panel: the Claude session can call the AIWorkHub manager tools, and
  a message is no longer shown twice in the transcript.

## 0.11.64 — 2026-09-23

### Added

- Manager panel: the model list fills itself from the enabled models of the
  chosen backend.

### Fixed

- Manager panel: the loop's writes no longer fail with `write_gate_closed`;
  it runs in its own gated MCP child started by Start and stopped by Close.

## 0.11.63 — 2026-09-23

### Added

- Dashboard: a Manager chat panel. Start a manager session on claude_cli,
  codex_cli or opencode_cli, talk to it, and watch its replies, tool calls
  and automatic callback wake-ups stream in.
- Bundled runtime: task callbacks wake an active manager session by
  themselves; Claude Code usage appears in the cost ledger; manager tools
  return compact summaries by default.

### Fixed

- Bundled runtime: validation-only replay, Windows LSP enrichment and GLM
  reviewer prose replies no longer block the task system.

## 0.11.62 — 2026-09-23

### Fixed

- Bundled runtime, Windows: rework launches no longer die before the model
  runs. The worker prompt now reaches `claude_cli` and `codex_cli` through
  stdin instead of the command line, which `CreateProcessW` caps at 32767
  characters (NF-2026-00042).

## 0.11.61 — 2026-09-23

### Added

- Bundled runtime: a host-side launch plan for the manager seat, so the
  manager CLI backend can run a turn on Windows. Workers stay confined.

### Fixed

- Bundled runtime, Windows: an AppContainer launch failure names its Win32
  cause, the executable and the command-line and environment sizes, never their
  values; an over-long command line is refused by name.
- Bundled runtime, Windows: the validation lane skips the seven host-privileged
  AppContainer tests with a named reason instead of failing every card that runs
  them.
- Bundled runtime: Source Graph no longer indexes nested linked git worktrees.

## 0.11.60 — 2026-09-22

### Added

- Bundled runtime: the provider-neutral core of the AIWorkHub manager agent
  loop. A manager session is persisted with a bounded event log, rehydrated
  from a bounded brief (handoff, open cards, state, context, role rules), and
  rotated with a handoff the next session starts from, so the managing model
  can be changed without losing state. The chat panel and the server surface
  that use it are the next releases.

### Fixed

- A validation-only replay grant is honoured from any verified manager route,
  not only `codex`, so a Claude manager can recover a blocked card.

### Changed

- The shared development assets — roadmap, NeedFix backlog, tool recipes,
  skills, KB, AI memory and the repository config — now travel with a clone.

## 0.11.59 — 2026-09-22

### Fixed

- Bundled runtime, Windows: native `claude_cli` workers now run inside their
  repo-scoped AppContainer with their worker tools (NF-2026-00025,
  NF-2026-00033, NF-2026-00034).
  - The container SID is granted only the provider install (read) and the
    per-request worktree, home and temp (modify, revoked on close).
  - Worker launches get outbound internet only. Validation launches get no
    network.
  - The worker MCP server runs on the host, behind a per-request pipe. It
    trusts no file, path or import root that the container can write.
- Bundled runtime, Windows: AppContainer validation runs pytest and ruff inside
  the container. `git diff --check` runs on the host, hardened, with a guard
  that refuses any junction, symlink or hard link in the candidate worktree
  (NF-2026-00040).
- Dashboard:
  - The header strip is compact, with uniform tiles and clamped captions.
  - Each tile shows a status dot.
  - Only attention counters (Blocked, Review, Stale) are coloured.
  - Light-theme contrast now meets WCAG AA.
  - Rows show the task title rather than worker boilerplate.
- Bundled runtime: semantic edits cannot be redirected through a planted
  junction. C/C++ `include/` headers resolve, and `cmake`/`ctest` are trusted.
  Blocked-card rework recovery works on Windows (NF-2026-00031).
- Bundled runtime: reviewer prewarm can no longer wedge the launch queue
  (NF-2026-00027). Dispatcher health reports undelivered manager-inbox callbacks
  (NF-2026-00029).
- VS Code LM bridge:
  - Declared writable files stay readable during forced staging
    (NF-2026-00023).
  - A tool name that is not allowlisted gets one correction instead of failing
    the request (NF-2026-00032).

### Known issues

- Python in an admin-owned directory outside Program Files, such as
  `C:\Python312`, needs a one-time elevated
  `icacls "<python dir>" /grant "*S-1-15-2-1:(OI)(CI)(RX)" /T`. The launch error
  names it. Per-user, Program Files and Store installs need nothing.
- Only `claude_cli` is bridged so far. OpenCode, Codex and the Copilot CLIs are
  not.

## 0.11.58 — 2026-09-21

### Changed

- Bundled runtime: an SDLC case stage can no longer be recorded `ready` on a
  caller's say-so (NF-2026-00945). `ready` for Plan, Design, Build and Test is
  accepted only when the runtime proves it, read-only and bounded, from the
  repository's own canonical receipts (the bound task and its falsifiable
  contract, a `review_ready` candidate sealed by the current claim against that
  contract, the sealed validation evidence, and the coordinator's
  accepted-outcome receipt for that candidate), and the resolved evidence is
  stored with the stage receipt. The payload supplies only the Plan and Design
  content and, for Build and Test, exact `task_id`, `request_id` and
  `claim_epoch` pointers; verdict-shaped keys such as `passed`, `verdict` or
  `sha256` are refused, and whatever cannot be proven is refused with a typed
  `stage_evidence_refused:<stage>:<code>` reason and a next action.
- Bundled runtime: a recorded `ready` receipt is re-proven on every read. A
  receipt written before this gate, one whose stored evidence no longer hashes
  or re-derives, and one whose predecessor stage is no longer proven stay
  visible for audit but read as `unknown` with a reason. A case's `cycle` is
  `complete` only when all six stages are proven now.
- Bundled runtime: Deploy and Maintain remain explicit refusals rather than
  evidence gates. The SDLC Deploy and Maintain gates do not yet consume
  canonical deploy target allowlist, release/build provenance receipt, install
  receipt, rollback receipt, deploy approval policy, deployed release identity,
  observed outcome metrics or control-limit policy, so `ready` for either stage
  is refused with each missing producer named and is never inferred, and a
  case's `cycle` cannot report `complete`. `not_applicable` is refused the same
  way until a canonical policy registry exists.

### Fixed

- Bundled runtime: the isolated launch now puts the OpenCode worker's
  request-local `awh` MCP config into the worker's own process environment
  (NF-2026-00919). It is derived from the worker MCP runtime already generated
  for the request, spelled for the selected sandbox (mount aliases under
  bubblewrap, real host paths under Landlock and Windows AppContainer), and set
  as `OPENCODE_CONFIG_CONTENT` with project-level OpenCode config disabled;
  nothing is written to disk and no global OpenCode config is touched. The
  contract fails closed: a config that does not meet it refuses the launch
  before any supervisor or worker process spawns and records the launch failure
  with a typed `opencode_worker_mcp_config_<cause>` reason, an infrastructure
  fault rather than a model-quality failure. The config must be exactly the
  `awh` server plus the worker permission contract (deny by default, only the
  `awh` worker tools allowed); its `environment` may carry only the request's
  own binding variables, never a provider credential or another inherited
  variable; it is bounded to 16 KiB of ASCII JSON; and the generated source it
  is read from must be a regular, non-symlinked, current-user-owned file beneath
  the request HOME whose request, repository and audit paths match the launch.
  This is launch wiring only: whether a live OpenCode/Muse worker then connects
  to `awh` is not measured in this release (below).

### Not in this release

- Live OpenCode/Muse worker qualification and Windows runtime qualification.
  The launch wiring is covered by unit and integration tests that run the real
  launcher with the task store, git and the supervisor spawn replaced by
  stand-ins; the Windows AppContainer cases fake the host platform, the Win32
  probe and the Win32 API. No live OpenCode/Muse worker was run against the
  delivered config and nothing was run on a real Windows host, so both remain
  unmeasured.
- Evidence gates for Deploy and Maintain: the SDLC gates do not yet consume the
  canonical deploy, release and outcome proof those stages would need, so only
  the first four SDLC stages are gated.
- LSP index integration, the OpenCode manager callback, the full stage-gated
  Playbook lifecycle, and portable `.aiworkhub` data are pending and not shipped
  here. No reasoning-quality improvement is claimed: causal reasoning-quality
  measurement remains incomplete, and inferred successor progression is still
  pending.

## 0.11.57 — 2026-09-21

### Fixed

- Bundled runtime: a reviewer or rework Source Graph overlay partition now pins
  the exact base index generation it was built against (NF-2026-00946). The
  canonical base is published by atomic replacement, so a marker that named
  only the canonical path let every ordinary publication break every in-flight
  reviewer/rework overlay. The marker now records the base generation's device,
  inode, size and `mtime_ns` and pins that generation by hard link beside the
  partition (never a copy or a content hash); reads compose with the pinned
  generation and verify its identity, so a newer canonical generation never
  leaks into a sealed review and a replaced or mutated pin fails closed. Where
  hard links are unsupported the marker records that, and a later base shift
  fails with an explicit `composed_base_shifted_unpinned` reason.
- Bundled runtime: a manager can reroute a retained candidate after a
  zero-delta launch failure that `recover_blocked_rework` already moved back to
  pending (NF-2026-00778), e.g. a claim that failed on provider authentication
  before any model work. Authority is the newest task-bound canonical
  `claim_start -> launch_failed -> blocked_rework_recovery` chain matched against
  the card's recovery and retained-predecessor identity, plus the process ledger
  proving zero changed paths on this runner; card fields alone are never
  authority, and the authorization is one-shot.

### Not in this release

- LSP index integration, OpenCode manager callback and OpenCode/Muse worker
  qualification, the full stage-gated Playbook lifecycle, and portable
  `.aiworkhub` data are pending and not shipped here. No reasoning-quality
  improvement is claimed: causal reasoning-quality measurement remains
  incomplete, and inferred successor progression is still pending.

## 0.11.56 — 2026-09-21

### Changed

- Bundled runtime: the sealed-delta verifier that rework materialization already
  used is now the write-free `verify_rework_delta_artifact`, which authenticates
  a sealed delta and returns its exact plan; `materialize_rework_delta_artifact`
  delegates to it. Recovery therefore authenticates a collected candidate with
  the same verifier a successor materializes through.

### Fixed

- Bundled runtime: explicit manager recovery of a blocked task
  (`recover_blocked_rework`) can now recover a timed-out candidate whose
  worktree retention already collected, from the delta sealed when the attempt
  terminated (NF-2026-00594). Only a truly absent worktree lets that delta stand
  in for it, and only when its descriptor binds this exact repository, task,
  request and claim epoch to an intact, non-symlinked artifact directly beneath
  the runtime's `rework_deltas` directory whose packet holds exactly the
  terminal's hash-pinned changed paths. Recovery then pins the descriptor on the
  successor's rework predecessor, so the existing materializer restores the
  sealed bytes instead of regenerating them. A present, dangling, symlinked or
  foreign worktree path keeps every retained-worktree check; a tampered,
  missing, foreign or mismatched delta fails closed with a typed
  `retained_terminal_candidate_*` reason and leaves the task unchanged; and the
  clean-root escape refuses (`clean_root_rework_sealed_delta_available`) rather
  than discard authenticated sealed bytes.

### Not in this release

- LSP index integration, the OpenCode manager callback, provider-neutral
  completion of the stage-gated Playbook, Muse worker qualification, and
  portable `.aiworkhub` data are pending and not shipped here. Inferred
  successor progression and causal reasoning-quality measurement also remain
  incomplete.

## 0.11.55 — 2026-09-21

### Added

- Bundled runtime: Roadmap list and detail views carry a server-side
  `current_wave` projection: the one in-progress wave with declared goals and
  the highest target version, with each goal checked only when all of its exact
  tasks are finished. Truncated, ambiguous, unversioned, goal-less or malformed
  Roadmap evidence yields a typed `UNKNOWN` with its reason instead of a guess,
  and a wave whose target the installed version has passed while goals are
  still unchecked is flagged overdue.
- Bundled runtime: `aiworkhub_task_create` and
  `aiworkhub_task_create_from_template` accept an optional exact
  `wave_goal_binding` (`roadmap_id`, `goal_id`, `predecessor_task_id`) that
  makes the new card that goal's current task in place of its predecessor. The
  binding is stored on the card, refused up front when it cannot apply, applied
  to the Roadmap once, and repaired by the reconciler when writes are enabled
  and the Roadmap write was interrupted. It is never inferred from a title,
  topic, version suffix or prose.
- Bundled runtime: the reconciler completes an in-progress wave only when every
  numbered acceptance criterion is mapped to a goal and every exact current
  task of every goal is canonically accepted with its own verifier receipt.
  Pending work leaves the wave in progress; missing, archived, blocked,
  superseded or unverified evidence yields a typed `unknown`. The single
  transition is write-gated, refused if the wave's goals or criteria changed
  after the verdict, records the accepted receipts, and never reads an
  installed or released version.

### Changed

- The wave mini-roadmap popup takes its wave, target and per-goal verdict from
  the bundled runtime's `current_wave` projection instead of ranking Roadmap
  rows itself. It shows the installed version and the wave's target separately
  (marking a passed target overdue), never checks a goal the runtime did not,
  and never counts an archived or stale task row as completion.

### Fixed

- Bundled runtime: worker validation sandboxes now seed a repository file that
  a declared pytest module locates by a literal `Path(__file__)`-relative path
  without importing it, plus a JavaScript asset's tracked local `require`
  targets, so such a test no longer fails on a missing asset in a sparse
  worktree (NF-2026-00551). Only git-tracked, non-dot-prefixed files inside the
  repository are seeded, as validation support rather than allowed writes:
  private state and untracked files stay out, a path that escapes the
  repository or crosses a link fails closed, and paths no filesystem can hold
  are declined instead of raising an untyped OS error.

### Not in this release

- Inferred successor progression (a successor takes a predecessor's place in a
  wave goal only through an exact binding that a task declares), the full
  stage-gated Playbook, LSP index integration, causal reasoning-quality
  measurement, and Muse/OpenCode worker qualification remain incomplete.

## 0.11.54 — 2026-09-21

### Fixed

- The bundled Source Graph reader stays in standby when another authenticated
  MCP process owns the index build; a fresh canonical index no longer appears
  degraded solely from normal builder contention (NF-2026-00933).

### Not in this release

- Automatic mini-roadmap progression, full Playbook stage gates, LSP index
  integration, causal reasoning-quality measurement, and Muse/OpenCode worker
  qualification remain incomplete.

## 0.11.53 — 2026-09-21

### Added

- Bundled worker-attempt receipts persist the selected reasoning option and
  reported model context capacity, without claiming provider-internal effort.

### Fixed

- Bundled semantic-review prompts request hash-matched candidate overlays for
  omitted hunks in truncated packets and still fail closed on missing or stale
  overlay evidence (NF-2026-00931).
- Bundled task creation checks required outputs; terminal events distinguish
  validation failure from provider timeout and record typed rate limits.
- Dashboard snapshot quarantine detail is bounded.

### Not in this release

- Full Playbook stage gates, LSP index integration, causal reasoning-quality
  measurement, and Muse/OpenCode worker qualification remain incomplete.

## 0.11.52 — 2026-09-20

### Added

- The wave mini-roadmap now shows the current wave's goals as a live checklist
  joined to each goal's task states, rather than a static list.
- Bundled runtime: semantic review scope is bounded to a candidate's exact
  changed segments, leading with the authenticated changed hunks and only the
  graph-connected callers and tests in the scoped audit, and failing closed on
  missing or stale changed-segment evidence.
- Bundled runtime: Source Graph ships a bounded LSP transport with fail-closed
  definition classification over a private workspace. LSP index integration is
  not included.

### Fixed

- Bundled runtime: blocked-rework recovery lets a strictly later terminal
  failure supersede a stale retained predecessor, re-deriving the predecessor
  from the failure's sealed delta (NF-2026-00515).

### Not in this release

- LSP index integration, the full stage-gated Playbook, reasoning matched to
  an accepted outcome, and Muse/OpenCode worker qualification remain
  incomplete.

## 0.11.51 — 2026-09-20

### Added

- Bundled SDLC case receipts and MCP surfaces link stages to exact canonical
  task identities. Full stage-gated Playbook transitions remain in progress.
- VS Code LM records the reasoning option actually handed to `sendRequest`
  and the model-reported context capacity, with provider-internal state kept
  unknown. Accepted-task metrics distinguish complete, incomplete and
  unverified event histories.

### Fixed

- OpenCode worker MCP registration uses the bounded `awh` alias without a
  duplicate legacy alias. VS Code LM worker requests retain required outputs
  and recover from oversized tool input.
- Manager cost-ledger summaries report bounded coverage; Source Graph retains
  JS/TS call-site coordinates for later LSP qualification. The LSP resolver
  and Semantic Review delta scope are not shipped as complete features.

## 0.11.50 — 2026-09-19

### Added

- The identity strip includes a wave mini-roadmap info popup for the
  current in-progress, current or active Roadmap wave.
- VS Code language-model requests apply a declared effort option only
  when the selected model exposes selectable keys that honor the
  canonical profile; otherwise the host records unsupported,
  provider-default, unverifiable or capability-ceiling and does not
  claim an applied value. Model context capacity is recorded from
  `maxInputTokens` and is never used to pad the prompt. The bundled
  runtime also applies the same verified effort and context receipts on
  CLI worker launches.

### Fixed

- The bundled reviewer retained stream also compacts
  `assistant.reasoning_delta` events so a long thinking turn does not
  refuse a completed review.
- Sparse-worktree VS Code raster fixture seeding and the sandbox-safe
  OpenCode config check are repository-only test/manager work and are
  not packaged as VSIX features.

## 0.11.49 — 2026-09-19

### Added

- Outcome-linked NeedFix metrics are included in the bundled runtime; the
  source repository release includes a receipt-authenticated accepted-task
  evaluation corpus, which is not packaged in the VSIX.

### Fixed

- OpenCode's global MCP registration uses the short `awh` alias and is not
  pinned to the AIWorkHub repository.
- The bundled SDLC metrics reader uses the shared read-only SQLite connector
  for paths containing URI-significant characters such as `#`.
- The Windows and OpenCode fixes in the 0.11.45-0.11.48 development notes below
  ship together here; those intermediate numbers were not separately tagged.

## 0.11.48 — 2026-09-16

### Fixed

- OpenCode's model list in Settings was still missing many installed models
  (nemotron and several others among them) even after last release's cache
  fix -- the compact list shared its row budget unfairly across providers, so
  a provider you had individually toggled models for many times before could
  crowd out one you had barely touched yet. The budget is now large enough
  that today's full catalog fits without crowding anyone out.

## 0.11.47 — 2026-09-16

### Fixed

- OpenCode's model list in Settings now stays current regardless of which
  panel you opened first -- it previously only showed installed models after
  the Workforce view had loaded at least once in the same session.

## 0.11.46 — 2026-09-16

### Fixed

- Windows: worker launches no longer fail with an unexplained authority-key
  error. A freshly created key could still be refused by last release's
  security check because nothing had hardened its permissions to match; it
  now is, before the key is ever used.
- Windows: OpenCode now appears in model settings when it's actually
  installed, instead of being refused outright before it was ever looked for.

## 0.11.45 — 2026-09-16

### Fixed

- Windows: Claude Code can now hold the manager seat. The venv launcher
  interposes a redirector process between the MCP server and `claude.exe`;
  identity verification now walks past that one known hop instead of refusing
  because the direct parent process wasn't `claude.exe`.
- Windows: native-CLI sandboxing works on capable hosts again. A required
  security API was being looked up in the wrong system library, so every
  Windows 11 host reported AppContainer confinement as unavailable even when
  it wasn't.

## 0.11.44 — 2026-09-15

### Fixed

- Windows: creating a task no longer stalls. A child process that inherited the
  MCP server's JSON-RPC stdin pipe hung before running its own first
  instruction, so every `git` the coordinator ran burned its whole timeout and
  `aiworkhub_task_create` appeared frozen — it now answers in 0.09 s instead of
  120 s.
- Windows: installed tools are measured again. Node and Ruff reported empty
  version facts, so a card requiring `node>=20.0.0` and `ruff>=0.12` was refused
  as unwinnable on a machine that had Node v22.16.0 and Ruff 0.16.1.
- Windows: the authority key is protected properly. It no longer follows a
  symlink, and its owner and ACL are checked against the security descriptor of
  the open file rather than trusted unconditionally.
- Windows: the reconciler heartbeat is readable again, so health reports stop
  showing a durable status that is present as missing.
- Windows: reviewer cards no longer stay stuck in `processing` — every terminal
  intent read failed, so no reservation could ever be completed.
- Windows: connecting a repository works again. A valid project manifest was
  read as unreadable because Node reports no device for a path it can open.

## 0.11.43 — 2026-09-15

### Added

- The manager can now make small, hash-bound range edits directly through the
  MCP semantic-edit tools workers already use, instead of only reading and
  reasoning about code.
- A new read-only `aiworkhub_dashboard_skills` MCP surface reports measured
  skill-selection coverage: how many recorded receipts actually selected and
  injected a skill, and the streak of recent receipts that injected nothing.
  An unreadable skill store reports "not measured" with a reason instead of a
  healthy-looking zero.
- A new read-only attempt-trajectory export composes one request's audit
  history, process lifecycle, artifacts and usage into a single canonical
  JSON document, with unmeasured fields reported as `UNKNOWN` rather than
  guessed.

### Fixed

- Source Graph's `calls` mode now resolves a query to the one symbol that
  actually defines the queried name before returning call edges, instead of
  also matching every import, decorator or annotation that merely mentions
  it.
- The Windows sandbox report now names the exact measured cause native CLI
  execution was refused — AppContainer APIs unavailable, the execution path
  not wired to them, or the platform is not Windows — instead of one fixed
  blocker code. This does not change, and does not claim, which Windows
  routes are launchable.
- Oversized Source Graph analytic-mode results from the worker AI-tools MCP
  Source Graph route are now spilled to a repository-scoped store before
  being trimmed for display, so the full original stays retrievable instead
  of being discarded the moment it is truncated. The store has no eviction,
  TTL or size cap yet.
- A duplicate launch request for a task already attached to a live worker now
  returns the existing claim instead of recording a new blocked episode that
  could overwrite the original worker's ownership.
- The compact-counter formatter keeps its extraction seam self-contained for
  the test harness; the four-significant-digit rendering shipped in 0.11.42
  is unchanged.

## 0.11.42 — 2026-09-15

### Fixed

- The Models view keeps every provider visible and keeps the routes you
  explicitly configured, even when a bounded catalog read comes back short.
  Rows are selected per provider after all providers have been seen, and routes
  named in `.aiworkhub/config/models.json` are reserved under both the identity
  they were written with and the canonical policy identity the catalog row
  carries, so a vendor-keyed OpenCode decision still pins its own row. Past the
  hard ceiling, pins that do not fit are counted as refused rather than dropped
  silently.
- Model counts no longer present an upstream-truncated list as an exact total.
  Each row is labelled by the bound that produced it — `declared, not
  discovered`, `configured, origin unknown past the host bound`, `configured,
  past the source bound` — and the view names the host that cut the tail rather
  than attributing the editor host's cap to OpenCode rows.
- Compact counters keep four significant digits, so 1000 reads as `1k` and 1001
  as `1.001k` instead of collapsing to one label; a mantissa rounding up to 1000
  promotes its tier, so 999999999 reads as `1B`. The decimal separator follows
  your locale.

## 0.11.41 — 2026-09-14

### Fixed

- Windows native-CLI workers now launch inside the repo-scoped AppContainer
  profile and its kill-on-close Job Object: the launcher declares that backend
  with the canonical repository identity and refuses to spawn without it, and
  the supervisor accepts no other spelling. The confinement report states the
  boundary actually in force rather than a fixed answer, so a host that does
  not qualify is still reported as bounded by process-tree lifetime only.
  Proved by fake-Windows behaviour tests over the real launch path; no
  live-Windows execution evidence is claimed yet.
- A Windows extension host resuming from idle no longer loses repository
  discovery to one transient handle, sharing or lock fault. The manifest read
  now retries exactly once, only for an authenticated transient cause, on a
  brand-new descriptor that repeats every symlink, regular-file and identity
  check. A missing, malformed, foreign or otherwise invalid manifest still
  fails closed on the first attempt, with no sleep and no second retry.

## 0.11.40 — 2026-09-14

### Fixed

- Rejected validation-only candidates can no longer loop through provider-free
  replay; the next rework claim must run the selected worker.
- Parent rejection no longer finalizes or cancels unrelated reviewer tasks.

## 0.11.39 — 2026-09-14

### Fixed

- Automatic review recovery now advances the pending reservation cursor through
  the action actually selected. A deferred first chain no longer loops back to
  itself and prevents later ready review chains from running in the same pass.

## 0.11.38 — 2026-09-14

### Fixed

- Automatic review recovery now advances up to six reservable lifecycle
  actions per reconciler pass and continues across deferred chains, eliminating
  the one-action starvation loop while retaining bounded interactive headroom.
- Task MCP writer serialization, validation replay authority, reviewer process
  cleanup and callback initialization fixes reduce recurring mechanical
  failures and SQLite lock contention.

## 0.11.37 — 2026-09-14

### Fixed

- Reconciler review recovery advances one durable lifecycle action per scan so
  stalled-review repair no longer blocks unrelated Task MCP operations for
  several minutes.

## 0.11.36 — 2026-09-13

### Fixed

- Toolchain receipt production and validation now share the same bounded file
  fingerprint, so large real-world executables no longer trigger false identity
  drift during provider-free replay.

## 0.11.35 — 2026-09-13

### Fixed

- Durable toolchain snapshots are executable-identity checked before reuse;
  stale cache facts are re-derived rather than poisoning provider-free
  validation replay before its first declared command.

## 0.11.34 — 2026-09-13

### Fixed

- Validation-only replay preserves non-empty `read_first` requirements in the
  signed toolchain identity, avoiding false cache-identity failures before
  validation command 1.

## 0.11.33 — 2026-09-13

### Fixed

- Repeated validation-only replay retains the exact authenticated worker MCP
  gate through mechanical validation failures, with fail-closed identity and
  retained-path binding.

## 0.11.32 — 2026-09-13

### Fixed

- Validation-only replay accepts the exact authenticated retained delta when
  the canonical parent has advanced, without widening ordinary unchanged-file
  allowances.

## 0.11.31 — 2026-09-13

### Fixed

- Retained-candidate validation replay carries the full signed task identity,
  eliminating false `toolchain_authority_receipt_card_identity_mismatch`
  failures while keeping receipts request-bound.

## 0.11.30 — 2026-09-13

### Fixed

- Reconciliation materializes missing automatic-review children for sealed
  review-ready candidates, closing the zero-child review stall without manual
  reviewer launches.
- Mechanical review parks, retained-delta reroutes, pending launch failures and
  blocked-review learning identities preserve their authoritative state across
  retries.
- Read-only analysis and research complete without mutation-only validation or
  reviewer requirements.
- OpenCode isolated workers receive authentication, support classic Snap under
  Landlock, and record capacity refusals as explicit provider evidence.
- VS Code LM finalization preserves valid partial finals while failing closed
  on contradictory or unauthenticated terminal evidence.

## 0.11.29 — 2026-09-13

### Added

- Repository Settings displays discovered OpenCode identities as exact model
  children under a collapsible OpenCode family instead of flattening every
  provider and model into one level.

### Fixed

- The Models snapshot reuses cached environment-preflight discovery, avoiding
  a duplicate `opencode models` process on each Settings refresh.
- VS Code LM quality reviewers now have one request-bound terminal submission
  contract, removing the conflicting printed-JSON instruction that caused
  provider-independent review failures.
- Reconciliation restores missing automatic-review chains for sealed
  `review_ready` candidates and no longer lets stale manager-ready projections
  starve current work.
- Every worker backend now enforces the same monotonic hard timeout, and the
  validation sandbox preserves explicit pytest project imports without
  weakening trusted-runtime ordering.

## 0.11.28 — 2026-09-12

### Fixed

- Completed automatic-review receipts remain discoverable after reviewer-card
  archival, preventing already-finished correctness and security work from
  being reported as missing at manager acceptance.

## 0.11.27 — 2026-09-12

### Fixed

- Automatic quality review can fail over to another eligible reviewer route
  after a mechanical route failure and can recover authenticated chains that
  an older runtime stopped only because no route was available.
- OpenCode JSON/SSE event capture now separates top-level terminal evidence
  from child sessions and deduplicates token, cache, and cost totals.

### Added

- A fail-closed OpenCode CLI runtime foundation with exact provider/model
  identities, Linux executable resolution, JSON command construction, and a
  request-local default-deny worker tool policy.

### Limitations

- OpenCode workforce discovery, task-route wiring, dashboard model selection,
  and a live canary remain pending; the adapter is not production-selectable
  in this release.
- Cross-process SQLite single-writer ownership remains open.

## 0.11.26 — 2026-09-11

### Fixed

- `record_launch_blocker` tolerates an unready/absent storage manifest
  instead of a fail-closed storage error masking the real launch-rejection
  reason.
- Workforce catalog rows now carry explicit manager/implementation-worker/
  reviewer role booleans with safe legacy defaults; Codex routes are always
  manager-only.
- The AppContainer-supervisor-identity launch seam and its platform
  dependency are declared in both governance gates that track this.

### Added

- Research: a provenance-pinned Ponytail adoption contract added to the
  universal-development-skills artifact.

### Known issues

- Four pre-existing regressions remain open (tracked as NF-2026-00796 and
  NF-2026-00798): automatic quality-review launch, a toolchain-cache
  finalization check, MCP write-gate visibility, and Source Graph bootstrap
  ordering in `server.main()`.

### Validation

- Full suite: 10,188 passed, 7 failed (the known issues above), 45 skipped.

## 0.11.25 — 2026-09-10

### Fixed

- Worker and reviewer finalization now defer manager notification until the
  system-owned correctness, security, and code-quality chain is complete.
- One authenticated manager-ready aggregate binds the exact candidate and all
  reviewer evidence, then publishes one atomic callback for manager action.
- Review automation no longer accepts the target, closes its NeedFix, or blocks
  later chains while the verified manager decides.
- Launch-time output validation now uses the persisted expanded-template
  provenance instead of misclassifying valid task contracts.
- Legacy review receipts remain compatible and are excluded from the new
  manager-ready queue.

### Validation

- The complete Python suite passes: 10,200 passed and 44 skipped.

## 0.11.24 — 2026-09-10

### Fixed

- Windows Source Graph refresh safely recovers a stale retained identity only
  after a non-signalling PID absence proof; live, recycled, and unprovable PIDs
  remain fenced and are never signalled.
- Shutdown mutates retained ownership only for the exact locally owned process,
  preventing reload from persisting a foreign `stopping` fence.
- Stopped/fenced refresh is now reported as non-refreshable and cannot pass code
  preflight merely because an older generation remains readable.

### Validation

- The Windows lifecycle simulations and focused Source Graph/preflight suite
  pass on Linux; owner-machine Windows live qualification remains pending.

## 0.11.23 — 2026-09-10

### Fixed

- Automatic quality-review orchestration no longer waits on a Source Graph
  partition receipt that can only be created by the reviewer launch itself;
  launch-owned prewarm still fails closed before provider execution.
- Dashboard foundation telemetry stays compact and opens its full Skills,
  Tool Recipes, and Semantic Edit evidence in an accessible popup.

### Added

- A source-audited design for universal, provider-neutral development skills
  and their future A/B evaluation is documented.

### Limitations

- Manager-ready notification still needs to be delayed until the automatic
  reviewer chain has completed (NF769).
- Source Graph durable single-owner (NF761) and the secure native
  SemLock-capable validation lane (NF690) remain open.

## 0.11.22 — 2026-09-10

### Fixed

- Dashboard full-snapshot hydration now includes Skills, Tool Recipes, and
  Semantic Edit.
- Canonical `task_queue.sqlite` mutation paths now take a single
  cross-process writer lease while concurrent readonly access remains
  available.
- SemLock preflight denial now emits exact per-command validation receipt
  cardinality.

### Limitations

- Source Graph durable single-owner (NF761) remains open.
- The secure native SemLock-capable validation lane (NF690) remains open.

## 0.11.21 — 2026-09-10

### Fixed

- Authenticated parse-broken repair launch now accepts request-scoped
  overlay evidence at prefetch, hides stale canonical symbols, and fails
  closed on identity, hash, or scope mismatch.
- Dashboard Tool Recipes, Skills, and Semantic Edit telemetry classify
  unavailable evidence separately from a measured zero (NF722).
- Compatibility repair (NF757).
- Release CI provenance requires a completed successful push run for the
  exact tag commit.

## 0.11.20 — 2026-09-10

### Fixed

- Source Graph `deadmethods` now reports exact entrypoint truth (NF568).
- Nested quality-review findings now normalize to the canonical finding
  schema (NF747).
- Quality review now delivers a single reviewer packet (NF748).

## 0.11.19 — 2026-09-09

### Fixed

- Worker, review and rework stages now preserve the authenticated request
  identity so later stages stay on the same request.
- Authenticated parse-broken rework prefetch (NF736) loads the retained
  candidate instead of asking the worker to rediscover it.
- Bounded missing-create finalization (NF737) stops after one exact
  correction when the same required create is still missing.
- VSIX packaging validation stays scratch-contained (NF745) and does not
  write outside the bounded workspace.
- The Marketplace landing page is restored as a complete page with the
  repository screenshot and architecture assets.
- Root generated `data/` JSONL hygiene (NF573) classifies root `data/` as
  artifacts and skips root `data/*.jsonl` in untargeted bodygrep before
  content/result-budget consumption, while targeted queries and nested
  package data remain available.

## 0.11.18 — 2026-09-09

### Fixed

- Reviewer launch and background packet preparation now agree on canonical
  review-ready state. State-less wait telemetry no longer blocks a valid
  quality review before provider start, and stale targets remain rejected.

## 0.11.17 — 2026-09-09

### Fixed

- Repeated identical missing required-create responses now stop with the typed
  `vscode_lm_finalization_nonprogress` result after one exact `v3_create`
  correction. If the rejected path changes, the new identity still gets one
  repair turn and can complete normally.

## 0.11.16 — 2026-09-09

### Fixed

- Python module validations no longer fail solely because the exact running
  interpreter comes from a hosted toolcache with permissive file mode; the
  trust exception remains identity-bound and other world-writable targets are
  still rejected.

## 0.11.15 — 2026-09-09

### Fixed

- Code workers with no required-output delta are now cancelled at the bounded,
  timeout-derived deadline with an exact terminal reason, preventing a warning
  from being followed by the remainder of a long runaway provider session.

## 0.11.14 — 2026-09-09

### Fixed

- Retained rework cards no longer fail before worker launch when review
  evidence names an already-landed system prerequisite outside the card's
  write scope; the prerequisite remains explicit in the sealed contract.

## 0.11.13 — 2026-09-09

### Fixed

- Editor-hosted reviewers submit durable verdicts through the bridge, and the
  workforce capability row reports that dispatch surface truth.
- Sandbox validation distinguishes unavailable Python semaphore support from
  candidate failure before running multiprocessing-dependent checks.
- Kilo/Grok usage captures nested cache counters and additive per-call cost
  without inflating snapshot token totals.
- Claude-only deferred-schema guidance stays out of every other worker prompt.

## 0.11.12 — 2026-09-09

### Fixed

- Workforce rows distinguish launch readiness from historical and current
  route execution evidence, including terminal provider failures.
- Text and native VS Code LM workers continue bounded semantic staging until
  every required edit/create is present and identify the exact missing path and
  action after each correction.
- Fully staged output finalizes locally without another model turn; repeated
  refusal ends with a stable semantic-stage error rather than a broad limit.

## 0.11.11 — 2026-09-09

### Fixed

- Native Codex workers receive an explicit role-scoped MCP `enabled_tools`
  contract, including Source Graph, semantic editing and validation.
- Codex launch now fails closed on a missing code-worker tool contract, while
  Claude-only deferred-schema guidance stays limited to the Claude CLI.

## 0.11.10 — 2026-09-09

### Fixed

- A pending retry with a retained rework delta can be rerouted to an available
  workforce route without discarding the candidate.
- Bare Python validation commands resolve to the trusted canonical interpreter
  before the worker sandbox runs them, matching coordinator finalization.

## 0.11.9 — 2026-09-09

### Added

- Workers are now told which AIWorkHub tool replaces each thing they are told
  not to do, instead of only being told not to do it.
- A worker that genuinely has to make a raw edit can declare why, and the
  declaration is recorded. Taking one of the three legitimate exceptions no
  longer looks the same as ignoring the rule.
- How much of each run's editing went through the semantic editor is measured
  and attached to the result. It is a measurement, not a gate.
- A relaunch that cannot come out differently is refused, and the refusal says
  what to do instead.
- A reviewer sees the earlier rounds' findings for the same task, with line
  numbers only where the file has not changed since.

### Fixed

- Six of the nine model transports were told their tools were blocked when
  nothing blocked them, and three of those were told it while their tool
  surface refuses more completely than any flag could. Each transport is now
  told what is actually true for it.
- Workers no longer receive the raw file editor. The semantic editor does the
  same job with a hash check, and the raw one was being reached for first in
  95% of edits. Creating a new file is unaffected.
- Eleven contract and smoke gates that had never run now run.
- An accept or a reject is written into the session store, so a rework worker
  starts with its predecessor's decision instead of nothing.

## 0.11.8 — 2026-09-08

### Fixed

- The validation sandbox could not install its seccomp filter on a
  position-independent interpreter, which is what most systems ship. The
  process died without a message instead, so validation runs failed with no
  explanation on those machines.

## 0.11.7 — 2026-09-08

### Fixed

- Manager startup left a background thread running on every call, which made
  validation runs that fork unstable on smaller machines. The sweep now runs
  when someone asks for it, and the reconciler owns it otherwise.

## 0.11.6 — 2026-09-08

### Added

- A tool-recipe run surface with usage evidence: the Tool Recipes panel now
  separates recipes that have run from the ones nobody has used, and reports
  who ran each. Seven operator recipes ship as package modules, so they work in
  every repository AIWorkHub manages instead of only in its own checkout.
- An accept preview that answers what would block an acceptance before any
  candidate tree is built.
- Skill usage evidence: a decision now records which skills the card received
  and the actor that produced the outcome, so a skill's activation evidence is
  measured rather than typed.

### Changed

- Manager bootstrap sends its contract once per session and the live identity
  every time, and folds in the repository and task-health facts that used to
  need two more calls.
- Task, NeedFix and review tools answer with receipts instead of echoing back
  the text the caller just sent.
- Reviewers receive the diff and the validation output the review packet always
  promised, for the one lens they were launched for.

### Fixed

- A rework worker now receives the measured failure from the attempt that
  preceded it instead of rediscovering it.
- The automatic reviewer launch driver works again; reviewers no longer have to
  be launched by hand.
- Task usage records carry their topic, so cost and routing views stop
  reporting a third of all work as unknown.
- The operator recipe scripts are shipped with the package; they were never
  committed and could not run outside a developer's own working copy.

### Fixed (release plumbing)

- Release qualification could not run at all: it installed pytest without the
  parallel plugin the project requires, so every platform job failed in under
  half a minute without running a test.
- Two seeding tests measured the toolchain of the machine that ran them, so
  the release qualified locally and failed on CI.

## 0.11.5 — 2026-09-08

### Fixed

- Read-only research and quality-review tasks can finish after successful
  verification. Both acceptance paths now retain the required outcome receipt
  and continue to reject missing or mismatched evidence.

## 0.11.4 — 2026-09-08

### Fixed

- Validation commands can update timestamps on their authenticated scratch files
  through the metadata broker, including Python and Node file-descriptor calls.
  File ownership and path restrictions remain enforced.
- Windows job and file metadata structures now share canonical declarations
  across process supervision, file operations and temporary-file handling.
- Successful task rework preserves the authenticated candidate across review
  rejection and verifies retained artifacts before restoring the same attempt.

## 0.11.3 — 2026-09-07

### Fixed

- Skills are mined from the repository's own correction record instead of
  written by hand, and a candidate must recur across three separate cards
  before it is offered.
- A skill declared at a lower risk tier now applies to higher-risk work, and
  cards can finally declare which skills they want on the path they are
  actually created on.
- A transient provider failure, an expired credential and a real defect are
  told apart. Work is no longer discarded when a credential expires at the end
  of a run.
- A model the account cannot use is switched off after it fails, instead of
  taking another hundred launches.
- The dashboard cards say loading before they say nothing.
