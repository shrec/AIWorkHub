# awh — AIWorkHub native control plane (C++23)

Status: architecture baseline v1 (2026-10-04). Owner-approved direction:
strangler-proxy migration, full on-disk compatibility with `.aiworkhub/`,
Source Graph as the first ported subsystem.

This document is the contract every implementation card under
`AIWorkhubCli/` follows. Where it disagrees with the Python runtime, the
Python runtime is the behavioral reference (parity) but **not** the design
reference: section 2 lists the design mistakes that must not be ported.

---

## 1. Review of the owner specification

The owner's YAML spec (single native `awh` binary, C++23, CMake presets,
vcpkg, SQLite WAL, CLI11 / nlohmann_json / spdlog / asio / Catch2,
capability-based platform layer, `IAgentProvider`, verification independent
of provider completion, git worktrees, typed events, behavior-preserving
incremental port with parity tests) is adopted with these corrections:

| # | Spec item | Decision | Why |
|---|---|---|---|
| R1 | No MCP transport anywhere | **Added `awh mcp` as the primary surface.** | The product *is* an MCP server: ~214 tools consumed by VS Code, Claude and Codex. CLI commands and MCP tools are two front-ends over one command dispatcher. |
| R2 | Source Graph only a `context_engine` bullet | **First-class subsystem `sourcegraph`** (section 6). | It is the core of the product and the first port target. |
| R3 | Missing subsystems | **Added:** callbacks (outbox + delivery), NeedFix, Roadmap, KB / AI Memory / Session / Context Graph, review & quality gates, write/launch authorization, retention, recipes/skills, SDLC, manager loop. | "Preserve existing workflows" is impossible without them. They land in later phases (section 8). |
| R4 | New entity schema (`tasks, runs, ...`) | **Domain types are new; on-disk schemas stay Python-compatible until cutover.** Storage adapters map types onto the existing per-context databases. Schema v2 (normalized task card, no JSON blob) is a post-cutover migration. | The owner chose full compatibility: Python and `awh` share `.aiworkhub/` during migration. |
| R5 | One virtual interface per platform service | **One header per service, one `.cpp` per OS, chosen at build time.** Virtual interfaces only where runtime polymorphism is real: `IAgentProvider` and `ProcessRunner` (the scheduler test seam). | A build only ever has one OS implementation. An interface with one implementation is indirection with no payoff. |
| R6 | ConPTY / PTY in phase 2 | **Deferred.** The slot is reserved (`platform/terminal`), but nothing is built until a provider declares `pty_required`. | Every current route runs non-interactive structured output: `claude --output-format stream-json`, `codex exec --json`, `opencode run --format json`. |
| R7 | asio from day one | **Standalone asio (no Boost), introduced with IPC** (callback sideband, app-server mux, editor bridge) in phase 6. | Phases 0–5 need only stdio plus process pipes, which a reader thread per pipe handles. |
| R8 | `static_linking: preferred` | Windows: static CRT (`x64-windows-static`, `arm64-windows-static`). Linux: static third-party libraries plus static libstdc++/libgcc (musl fully static is optional). macOS: static third-party libraries only. | Apple's libSystem cannot be linked statically. |
| R9 | UI phase 1 "IPC-or-local-API" | **No HTTP listener.** The existing Webview keeps talking MCP through the extension's stdio child, which becomes `awh mcp`. | This is a deliberate current invariant (docs/ARCHITECTURE.md, "Dashboard: native Webview, not HTTP"). |
| R10 | Event bus | **Split.** Durable domain events go to the SQLite outbox *in the same transaction* as the state change. Ephemeral stream events (`process.stdout`) go to a bounded in-memory ring. | Mistake M9: a state change and its event committed separately. |
| R11 | Providers claude/codex/copilot/opencode | **Plus `vscode_lm`** (extension-hosted model, file spool, no Node in core), deepseek/glm through the copilot BYOK route, and kilo. | These are existing routes and workflows. |
| R12 | No sandbox module | **Added `sandbox`** with a declarative grant manifest (section 7). | The 40% of critical NeedFix records concerning AppContainer came from a sandbox that was bolted on. |
| R13 | No error / unknown / budget model | **Added to `base`** (section 3). | These are the top-ranked old mistakes. |

## 2. Rules that replace old mistakes (no carry-over)

Each rule cites the measured defect it prevents (see the evidence catalogue
in the manager session of 2026-10-04: NeedFix ids, LEDGER/AUDIT sections).

1. **Typed errors, never strings.** `std::expected<T, Error>` everywhere.
   `Error{domain, os_code, op, path, detail}` reaches the terminal record
   unchanged, with no `str(exc)[:500]` and no dropped Win32 codes. `catch (...)`
   is allowed only at thread roots, and it converts to `Error`. *(M18, M22)*
2. **Unknown is a value.** Use `Known<T>`/`Unknown{reason}`/`Unmeasured`. Unknown
   is never shown as 0, `alive` or `passed`. "Passed" requires `executed > 0`. Explicit 0
   is never coerced to a default (`optional<int>`, no `x or 30`). *(M13, M21, M23)*
3. **Library boundaries with a size ratchet.** Each subsystem is a static
   library and dependencies only point downward (enforced by CMake link
   graph). CI fails on a file over 800 lines or a function over 80 statements
   (`clang-tidy readability-function-size`). Nothing merges without a
   production call site plus an end-to-end reachability test. *(M1, M2)*
4. **One storage layer.** One connection policy per database class. A state change plus its
   outbox event go in one transaction. **No subprocess, scan or file I/O while
   holding a writer lease**: compute first, then apply. Migrations are keyed
   by `PRAGMA user_version`, transactional, and run once by the owning
   subsystem, never on every connect. FTS is rebuilt in the same transaction. *(M4–M10)*
5. **Every wait has a deadline from process start.** Waits are event-driven (job objects,
   IOCP, `waitpid`, condition variables). Polling is allowed only as a bounded
   reconcile fallback. Liveness without evidence degrades from
   `unresponsive` to terminal and never stays `alive`. *(M12, M13)*
6. **Sandbox as a module.** A grant manifest is applied and revoked per launch. One
   env-block builder (OS baseline plus allowlist) is unit-tested on the produced block. The
   handle-inheritance list is explicit and stdin is NUL unless declared. All I/O is binary. A native
   end-to-end smoke test is a release gate. *(M16, M17, M19)*
7. **Canonical ids, parsed once.** `RepoId`, `TaskId`, `RequestId`,
   `ModelId`, `RouteId` are value types, always repo-qualified, and alias
   normalization happens at the boundary only. *(M25–M27)*
8. **Real OS tests.** Every OS shim has a real-OS integration test. A
   fake kernel object is never the only test. Tests run in hermetic roots, so live
   `.aiworkhub/` paths cannot be reached from tests. *(M28, M29)*
9. **Bounded responses.** Every query and tool result has a byte budget and a
   cursor. Stored rows are projections, never transport envelopes. *(M5, M31)*
10. **One config, one authorization chokepoint, one admission controller.**
    A schema-validated config rejects invalid values (no silent clamping) and is
    dumped by `awh doctor`. `ALLOW_WRITES`/`ALLOW_LAUNCH` are checked once in the
    dispatcher, not per tool. Capacity is owned by one admission controller. *(M14, M35, M37)*
11. **Multicore by default.** The pool size comes from the observed core count minus headroom
    for the interactive server, never a constant. Parallel output is
    byte-identical to sequential output (deterministic merge order). A
    sequential path carries a comment with its measurement. *(project policy)*
12. **One version source.** `awh` reads the canonical version from
    `src/aiworkhub/_version.py` at configure time. Nothing is duplicated by hand. *(M36)*

## 3. Layering

```text
apps/awh            main.cpp, CLI11 command tree
  │
frontends           cli/  mcp/ (stdio JSON-RPC, proxy)   [ipc/ in phase 6]
  │
app                 dispatcher: Command{name, schema, handler, gates}
                    tool registry, authorization chokepoint, budgets
  │
services            sourcegraph  tasks  callbacks  needfix  roadmap
                    knowledge(kb, memory, session, context_graph)
                    workspace  sandbox  providers  scheduler  verify
                    evidence  retention  recipes  sdlc  manager_loop
  │
storage             sqlite (RAII, prepared stmts), writer_lease, migrate,
                    per-store repositories (Python-compatible schemas)
  │
platform            fs  process  lock  env  system  [terminal, ipc later]
                    win/*.cpp  posix/*.cpp  (macos deltas in posix/)
  │
base                Error/expected, Known<T>, ids, json, budget/cursor,
                    thread_pool, clock, log (spdlog → stderr only)
```

- Each box is a CMake static library and links only to boxes below it.
- A service never includes another service's internals; it calls the other
  service's public header. A cycle is a build error.
- In `awh mcp`, **stdout is protocol only**. All logging goes to stderr or a file.

## 4. Strangler proxy (how migration runs)

```text
VS Code ext / Claude / Codex
          │ stdio MCP
          ▼
      awh mcp ──────────── native tools (registry, ported + parity-green)
          │
          └── child: python -m aiworkhub.server   (everything not yet native)
```

- **Routing.** `tools/list` returns the native schemas plus the child's schemas for
  names not native. Until cutover, a native tool's schema must be byte-equal
  to Python's frozen schema (fingerprints in `contracts/mcp_tools.json`).
- **Per-tool kill switch.** In `.aiworkhub/config/awh.json`,
  `native_tools: "all" | [names]` and `python_fallback: {command, args}`.
  A rollback is a config edit, not a release.
- **Subsystem ownership handoff.** `awh` passes `AIWORKHUB_NATIVE_OWNED=
  source_graph,...` to the Python child. Python skips daemons and writers for
  owned subsystems, and only one implementation ever writes a given store. Each handoff
  needs a small Python-side card that adds the guard.
- **Launch contract.** `awh mcp` accepts the extension's existing env
  (`AIWORKHUB_REPO_ROOT`, `AIWORKHUB_REPO`, `AIWORKHUB_REPO_ID`,
  `AIWORKHUB_WINDOW_ID`, `AIWORKHUB_CLAIM_EPISODE`,
  `AIWORKHUB_CALLBACK_TRANSPORT`, `AIWORKHUB_ALLOW_WRITES`,
  `AIWORKHUB_ALLOW_LAUNCH`, `AIWORKHUB_WORKTREE_ROOT`, coordinator token
  vars) and forwards it to the child unchanged. The extension gains a
  `aiworkhub.runtime: native | python` setting. Python stays the default
  until the phase 1 parity run is green.
- **MCP protocol.** NDJSON JSON-RPC 2.0. Supports `2024-11-05` (current
  fallback) and the current spec revision by negotiation. The 8 MiB frame
  cap is kept.

## 5. Storage compatibility contract

`awh` reads and writes the existing stores with their current schemas:

| Store | Path under `.aiworkhub/` | Journal |
|---|---|---|
| tasks | `tasking/task_queue.sqlite` | WAL |
| needfix, roadmap, skills, tool_recipes | `tasking/*.sqlite` | WAL |
| kb / memory / sessions / transcript graph | `kb/knowledge.sqlite`, `memory/memory.sqlite`, `sessions/*.sqlite` | per store (frozen in P0) |
| source graph | `source_graph/source_graph.sqlite` | DELETE plus staged-generation publish |
| sdlc | `sdlc/cases.sqlite`, `runtime/sdlc_sync.sqlite` | WAL |

- **Writer lease.** The lease is byte-compatible with `db_writer.py`: an OS advisory lock on
  `<db>.writer.lock`, the same lock primitive and byte range (Windows
  `msvcrt.locking` ⇒ `LockFileEx` on the identical range; POSIX primitive
  frozen in P0), a 30 s timeout, and a typed retryable `WriteLeaseTimeout`. Python and
  `awh` must exclude each other. A cross-implementation contention test is
  mandatory.
- **Other lock files.** These are honored identically: `source_graph/index.lock`,
  `build-process.{json,lock}`, `runtime/locks/task_reconciler.lock`,
  `.request-locks/<id>.lock`, `.promotion.lock`, `<ledger>.append.lock`.
- **Schema v2** (normalized task card, typed evidence tables) is designed now
  and applied only after cutover through `user_version` migrations.

## 6. Source Graph (first ported subsystem)

The goal is the same answers (or better), measured, in a fraction of the time.

**Index (phase 2a)**
- **Parser.** tree-sitter for *every* semantic language: Python, C, C++ (also
  CUDA, OpenCL and Metal through the C++ grammar), JS, TS, TSX, Rust, Go,
  Java, C#, PHP. This replaces Python's stdlib-AST adapter and the regex/lexical
  adapters. Definitions and references come from each grammar's upstream
  `queries/tags.scm` plus local overrides. Grammars are compiled **statically
  into `awh`** from vendored sources pinned by hash in `third_party/grammars/`.
  That removes the grammar cache, its sandbox mirror and its ACL
  grants (`tree_sitter_cache.py`).
- **Non-semantic families** (JSON, YAML, TOML, XML, Markdown, …) keep
  truthful path/language/size/hash `FILE_EVIDENCE` only.
- **Pipeline.** The pipeline runs in four steps:
  1. Walk the tree with `.gitignore` plus repository policy (`config/source_graph.json`). Linked worktrees are detected by a `.git` *file* (M32).
  2. Parse in parallel (pool = cores − headroom).
  3. Merge in sorted path order.
  4. Do one write transaction into a staging DB, then the staged-generation publish.

  Git history (churn/ownership) comes from one `git log --numstat` call **before**
  the transaction (M10).
- **Crash isolation.** The build runs as a child `awh sg build --staging <path>`.
  A grammar crash cannot take down the MCP server.
- **Schema.** `aiworkhub.source_graph.v1` tables are written exactly
  (`meta, files, entities, edges, file_history, index_quality_history,
  lsp_*`). Before the native build revision is published, P0 freezes how Python
  readers react to a new build revision. A rebuild war between two daemons is
  prevented by the ownership handoff.

**Semantic layer: LSP (phase 2b)**
- **Why both layers.** tree-sitter gives every file a syntactic graph in every
  environment, deterministically and with no external program. Name matching
  alone leaves most call edges unresolved. The Python index on this repository
  resolves 27% of 501,945 edges, and resolution is its slowest phase (19.8 s
  of 36.7 s, measured 2026-10-04). A language server resolves those edges the
  way the compiler does. LSP does not replace the parser. Servers are external
  programs, they need project configuration (compile_commands.json, a venv, a
  tsconfig), and they may be absent, as in the sandboxed validation lane. An
  absent server never blocks or fails an index. It yields typed
  `server_unavailable` evidence (rule 2).
- **Servers.** Each language's server is explicit configuration in
  `config/source_graph.json` (command, args, version probe). Nothing is
  downloaded during a refresh. Targets: clangd (C, C++, CUDA through
  compile_commands.json), basedpyright or pyright (Python),
  typescript-language-server (JS, TS), gopls, rust-analyzer, jdtls, csharp-ls.
  Python wires only Pyright and typescript-language-server today, and only for
  definitions. This host has only clangd 21 installed, so the Python
  enrichment does not run here.
- **Requests.** LSP 3.17, negotiated per server capability.
  `textDocument/definition` resolves edges, as it does today. Where the server
  advertises them, the layer also uses `callHierarchy/incomingCalls|outgoingCalls`,
  `textDocument/references`, `textDocument/implementation` and
  `typeHierarchy/supertypes|subtypes`. The position encoding is negotiated
  (utf-8 preferred). Conversion from the index's UTF-8 byte columns happens at
  the boundary.
- **Pipeline position.** The layer runs after the syntactic parse and lexical
  resolution, before the write transaction and never inside it. The batch is
  the unresolved and ambiguous call edges, plus the entities that `calls`,
  `impact` and `trace` need. Sessions run in a pool sized from cores with
  headroom. Their workspace is bounded and excludes `.aiworkhub`, worktrees
  and `node_modules`. Every request and the whole batch have deadlines. Results
  are sorted, so completion order cannot change the output.
- **Provenance.** Each enriched edge records source and target hashes, the
  server name and version, the config digest and a classification
  (`repo_internal`, `external_stdlib`, `external_dependency`, `unresolved`,
  `ambiguous`, `server_unavailable`) in the existing `lsp_*` tables. A changed
  file, server or config revokes stale enrichment. A lexically resolved edge is
  never overwritten to raise a ratio. An external target is evidence, not an
  in-repo edge.
- **Process control.** The LSP client reuses `platform/process` (handle list,
  Job Object) and the JSON-RPC core of the MCP proxy. Only the framing differs:
  `Content-Length` headers instead of NDJSON. Python's Windows LSP concurrency
  failures (NF-2026-00038) do not carry over.
- **Health.** Health reports typed per-language counts with denominators:
  attempted, internal, external, ambiguous, unresolved, unavailable, stale and
  latency. A missing server is never reported as green.
- **Only if measured.** Query-time escalation (a warm session answering for
  the focused symbol) and SCIP index ingestion are added only when the
  retrieval eval shows a gap that batch enrichment leaves open.

**Queries (phase 2c)**
- All 37 modes are pure functions over a read-only snapshot connection. They share one
  ranking core and one budget/cursor implementation. The response shapes, evidence
  labels and receipt fields are the frozen Python contract.
- Manager and worker surfaces (`*_source_graph_query`, health, refresh,
  ensure_started, stop, retrieval_eval) become native tools.

**Daemon and partitions (phase 2d)**
- The refresh scheduler uses OS file watching (ReadDirectoryChangesW / inotify /
  FSEvents) with a bounded periodic reconcile as fallback (rule 5).
- The worker partition is a composed view over a pinned base generation
  (`source_graph_partition.py` semantics).

**Parity gates for Source Graph**
1. On the retrieval eval corpus (`.aiworkhub/source-graph-retrieval-eval.json`),
   precision@k, recall@k and MRR must be at least the Python values for every query class.
2. Shape parity per mode on frozen fixture repos (Python, C++, mixed),
   with volatile fields normalized.
3. Measured build time and peak memory against Python on this repository (1,188
   files) and on a C++ repository (MegoProject). The numbers are recorded, not claimed.
4. LSP enrichment on the eval's `calls` and `impact` cases. In-repo precision
   and recall must be at least Python's, with zero false-zero answers. Server
   availability and coverage are reported per language. C and C++ through
   clangd are measured on MegoProject and on `AIWorkhubCli` itself.

## 7. Process, sandbox and providers (phases 4–5)

- **`platform/process`.** Windows uses `CreateProcessW` with an explicit
  `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` and a Job Object (`KILL_ON_JOB_CLOSE`) for
  tree cleanup. POSIX uses `posix_spawn` with a new process group and `killpg`. One
  reader thread per pipe feeds the ephemeral event ring. Cancellation is
  graceful and then forced, with a deadline.
- **`sandbox`.** A `GrantManifest{read_exec: [...], read_write: [...],
  network: bool}` is derived from the card (allowed_writes, provider install
  root, worktree, home, temp). Backends are AppContainer and restricted token
  (Windows) and Landlock or bubblewrap (Linux). Grants are applied before spawn and
  revoked at teardown. **The verifier's diff-vs-allowed_writes check is
  authoritative. The sandbox is defense in depth**, so a sandbox gap cannot
  make an out-of-scope write pass review.
- **`providers`.** `IAgentProvider` with the normalized request/result from the
  owner spec, plus a capability record (`resumable_sessions`,
  `structured_output`, `streaming`, `tool_events`, `pty_required`, …). Each
  adapter has its own circuit breaker (M15). Provider completion is never
  authoritative. Every result goes through `verify`.

## 8. Phases and exit criteria

| Phase | Scope | Exit (measured) |
|---|---|---|
| **P0 Spec freeze** | Contract artifacts in `AIWorkhubCli/contracts/`: all tool schemas plus fingerprints, `sqlite_master` of every store, task FSM table, lock-file protocol (exact primitives/ranges), env contract, Source Graph reader revision behavior. Black-box **MCP record/replay parity harness** with a parameterized server command and volatile-field normalization (generalizes `tests/mcp_stdio_client_smoke.py` C1–C6). | Harness runs green Python-vs-Python on fixture repos. Contracts regenerate deterministically. |
| **P1 Foundation** | CMake presets, vendored deps, CI matrix (win-x64, linux-x64, linux-arm64, macos-arm64). Libraries: `base`, `platform` (fs, lock, env, minimal process), `storage` (sqlite, writer lease, migrate). `awh mcp` with transparent Python proxy; `awh call <tool> '{json}'`, `awh version`, `awh doctor` (skeleton). | The parity harness through `awh mcp` (100% proxied) is identical to direct Python. The cross-implementation writer-lease contention test is green. |
| **P2 Source Graph** | 2a syntactic index (tree-sitter), 2b LSP semantic layer, 2c 37 query modes plus tools, 2d daemon, watcher, partitions, Python ownership guard. | The section 6 parity gates. Native SG tools on by default. |
| **P3 Core state** | Tasks (read → write → FSM), callbacks outbox, needfix, roadmap, kb, memory, session, context graph, dashboard read builders. CLI: `awh task create/list/show`, `awh status`. | Per-tool parity green. Python ownership guards for each store. |
| **P4 Runtime and providers** | Process tree control, env-block builder, sandbox, providers (claude, codex, opencode, copilot, vscode_lm spool), routing/workforce/admission. CLI: `awh agent list/doctor/run`. | Native end-to-end smoke per provider per OS. Cancellation leaves no orphan processes (tested). |
| **P5 Workspace and verify** | Worktree manager, provisioning (content-addressed templates, M20), diff collector, validation runner, evidence, review/quality gates, semantic edit. CLI: `awh verify/review/promote`, `awh task run/cancel`. | The accept/reject flow on a real card is identical to Python. |
| **P6 Daemons and IPC** | asio, reconciler, dispatcher/callback delivery, callback channel, app-server mux, manager loop, retention. CLI: `awh session list/show/resume`, `awh logs`, `awh config`. | Callback delivery end to end for Claude and Codex with no polling path. |
| **P7 Cutover** | The extension spawns only `awh`. The VSIX ships native binaries per platform. The Python child is removed. Schema v2 migrations. | Starts with no Python and no Node in core. Full parity suite green on the matrix. |

The owner's CLI tree (`init, status, doctor, task *, agent *, session *,
verify, review, promote, logs, config, version`) lands with the phase that
owns each subsystem. Every MCP tool is also reachable as `awh call`.

## 9. Directory layout

```text
AIWorkhubCli/
  ARCHITECTURE.md  CMakeLists.txt  CMakePresets.json
  cmake/                    toolchain + warnings + size-ratchet helpers
  contracts/                P0 frozen artifacts (generated, checked in)
  src/
    base/ platform/{win,posix}/ storage/
    sourcegraph/{index,lang,query,daemon,partition}/
    mcp/ cli/ app/            (+ later service dirs per phase)
    main.cpp
  third_party/              vendored, pinned sources (README lists sha256)
  tests/{unit,integration,parity}/
```

## 10. Build and dependencies

- **Toolchain: LLVM on every OS.** C++23, CMake ≥ 3.28, Ninja. Presets
  `windows-clang` (clang-cl, lld-link, llvm-rc, llvm-mt over the MSVC STL and
  Windows SDK headers, static CRT), `linux-clang` and `macos-clang`, each with a
  same-named build and test preset; validation uses only presets, so no
  build-tree path is ever named. One compiler family gives one set of
  diagnostics and one C++23 behaviour everywhere. Measured 2026-10-04 on this
  host (LLVM 21.1.0): clang-cl builds C++23 `std::expected`/`std::format` with no
  developer shell (INCLUDE/LIB unset), the static binary imports only
  `KERNEL32.dll`, and Ninja writes `compile_commands.json`, which clangd needs
  (phase 2b) and the Visual Studio generator does not produce. MSVC `cl` is not
  a build target; a CI lane adds it only for a measured portability defect.
- **Vendored dependencies** in `third_party/` (see its README for the pins and
  sha256). There is no package manager at build time, so builds are hermetic
  and work in the sandboxed, network-less validation lane, which also rejects
  absolute host paths such as a vcpkg root. Vendored: sqlite3 amalgamation
  (FTS5 enabled), CLI11, nlohmann_json, spdlog (`SPDLOG_USE_STD_FORMAT`, no fmt),
  Catch2 amalgamated, tree-sitter core. asio arrives in P6.
- Warnings are errors on our code only (`/W4 /permissive-` plus `-Wextra`
  through clang-cl, `-Wall -Wextra -Wpedantic` elsewhere). The same toolchain
  supplies ASan/UBSan, libFuzzer and source-based coverage (`llvm-cov`) on every
  OS, Windows included; clang-tidy and clang-format are the lint gates.
- Required at runtime: `git`. Optional per provider: `claude`, `codex`,
  `copilot`, `opencode`.

## 11. How work is executed

Implementation goes through AIWorkHub task cards (the manager reviews and does not write the code).
Cards whose `allowed_writes` overlap are sequential. Every card includes its
tests and production call sites in `allowed_writes`, and its validation is: configure,
build, unit tests, plus the parity subset it touches.
