# AIWorkHub — Bug & Optimization Audit Report

> Historical snapshot, not the current defect backlog. Current-HEAD dispositions
> are recorded in `docs/LEGACY_AUDIT_REVALIDATION_2026-08-10.md` and the canonical
> NeedFix registry. Performance estimates below remain hypotheses unless a current
> benchmark artifact verifies them.

**Date:** 2026-08-06
**Methodology:** Parallel subagent exploration (3 agents: Python bugs, Python optimizations, TypeScript/extension bugs)
**Scope:** Full repository — `src/aiworkhub/`, `vscode-extension/`, `scripts/`, `tests/`

---

## Executive Summary

| Category | Critical | High | Medium | Low |
|----------|----------|------|--------|-----|
| Python Bugs | — | 3 | 5 | 8 |
| Python Optimization | — | 2 | 6 | 4 |
| TypeScript/Extension | — | 3 | 6 | 4 |

**Overall:** Codebase demonstrates production-grade defensive programming. No critical (crash/data-loss) bugs found. Highest-value improvements are SQLite performance tuning (WAL mode for task store) and character-by-character string masking in the source graph AST parser.

---

## Part 1: Bug Findings

### 1.1 Python — Source Code (`src/aiworkhub/`)

#### HIGH

| # | File | Lines | Issue |
|---|------|-------|-------|
| H1 | `repository_bootstrap.py` | ~134–138 | Bare `except Exception` swallows critical interpreter errors (`MemoryError`, `RecursionError`) during source graph daemon startup. Should catch only `DaemonError`, `OSError`, `sqlite3.Error`. |
| H2 | `callback_store.py` | ~320–345 | `enqueue_callback()` catches `sqlite3.IntegrityError` but if `conn.commit()` raises a different error (e.g., `OperationalError`), the connection is left in an open transaction without rollback. |
| H3 | `task_engine.py` | ~355–380 | TOCTOU: `mark_terminal_review()` opens a separate connection for callback enqueue after the task store writes in its own transaction. The task could change state between transactions. |

#### MEDIUM

| # | File | Lines | Issue |
|---|------|-------|-------|
| M1 | `task_store.py` | ~100–115 | `_atomic_write_json` leaks file descriptor if `os.fdopen(fd, ...)` raises — `fd` is not closed in the error path. |
| M2 | `vscode_lm_bridge.py` | ~135–138 | `_atomic_json` finalizer catches only `FileNotFoundError` on `os.unlink` — `PermissionError` (Windows file lock) silently leaks temp files. |
| M3 | `task_store.py` | ~650–670 | `list_tasks` fetches ALL rows into memory then filters in Python — O(n) memory where bounded SQL could be used. |
| M4 | `dashboard_mcp_app.py` | ~370–430 | Dynamic SQL with f-strings based on `PRAGMA table_info` — fragile if schema has unexpected columns (not an injection risk). |
| M5 | `process_launcher.py` | ~1055–1070 | TOCTOU between `path.stat().st_size` check and `path.read_text()` — file could grow between check and read (observability-only, low risk). |

#### LOW

| # | File | Lines | Issue |
|---|------|-------|-------|
| L1 | `source_graph.py` | ~271–302 | Nested exception handling in `index_write_lease` — `ValueError` (from `seek` on closed fd) not caught in unlock path. |
| L2 | `callback_store.py` | ~88–95 | WAL-mode retry loop uses linear backoff without jitter — multiple processes could synchronize on retry. |
| L3 | `callback_bridge.py` | ~931 | `read_text()` without try/except — depends on caller handling. |
| L4 | `terminal_authority.py` | ~75–115 | Minor TOCTOU in key file management — `exists()` check then re-read, harmless false alarm. |
| L5 | `source_graph.py` | ~430–470 | `os.replace` without directory fsync (well-known POSIX limitation, accepted risk). |
| L6 | `worker_supervisor.py` | ~415–420 | `SIGBREAK` handler on Windows may silently never fire if process not attached to console. |
| L7 | `core.py` | ~170–235 | macOS degrades silently (no `/proc`) — returns `None` without comment. |
| L8 | `claude_auth.py` | ~78–85 | Temp filename uses `os.getpid()` — theoretical fork race (extreme edge case). |

### 1.2 TypeScript — VS Code Extension (`vscode-extension/`)

#### HIGH

| # | File | Lines | Issue |
|---|------|-------|-------|
| VH1 | `extension.js` | ~1950–1975 | `McpStdioClient._scheduleAutomaticRecovery` — `.finally()` can bypass `maxAttempts` budget when `_onExit` fires between `.then()` and `.finally()`. |
| VH2 | `extension.js` | ~1660–1680 | `ensureStarted()` TOCTOU — readiness check and actual start are not atomic. Recovery circuit can be bypassed if child exits on another microtask. |
| VH3 | `extension.js` | ~7310–7330 | `VscodeLmBridgeHost.stop()` doesn't guard `this.disposed` — double-stop after dispose can touch stale resources. |

#### MEDIUM

| # | File | Lines | Issue |
|---|------|-------|-------|
| VM1 | `runtime-retention.js` | ~74–76 | `fs.lstatSync` without try-catch — TOCTOU on concurrent filesystem mutation throws unhandled. |
| VM2 | `extension.js` | ~1430–1445 | `sanitizeWebviewPayload` regex doesn't match Windows absolute paths (only `/`-prefixed). |
| VM3 | `media/app.js` | ~2661 | `innerHTML` used as entity decoder (anti-pattern) — safe due to CSP but fragile. |
| VM4 | `extension.js` | ~2040–2060 | `_attachChildStreamErrorGuards` only logs stream errors, doesn't fail pending MCP requests. |
| VM5 | `extension.js` | ~3360–3380 | `VscodeLmBridgeHost.dispose()` + VS Code subscription disposal = two-stop risk. |

#### LOW

| # | File | Lines | Issue |
|---|------|-------|-------|
| VL1 | `extension.js` | ~996–1005 | `renewWindowRouteLease` silently swallows errors every 4 minutes indefinitely. |
| VL2 | `runtime-retention.js` | ~42–54 | `treeSize()` uses `fs.statSync` after `readdirSync({ withFileTypes: true })` — redundant stat calls. |
| VL3 | `native-launcher/main.go` | ~44 | Exit code extraction pattern is safe (double-checked type assertion). |
| VL4 | `extension.js` | ~2162–2181 | Module-level mutable state — safe under normal VS Code lifecycle. |

---

## Part 2: Optimization Opportunities

### HIGH IMPACT

| # | File | Issue | Fix | Expected Gain |
|---|------|-------|-----|---------------|
| O1 | `task_store.py` | Task store uses default SQLite journal mode (DELETE), no `busy_timeout`, no `synchronous` pragma | Add `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000` in `_connect()` | 2–5× concurrent throughput, eliminates `SQLITE_BUSY` |
| O2 | `source_graph_ast.py` | `_mask_php_non_code()` and `_mask_c_family_non_code()` use `list(text)` creating O(n) single-char string objects | Use bytearray or streaming state-machine approach | 3–5× faster, significantly lower memory for large files |

### MEDIUM IMPACT

| # | File | Issue | Fix | Expected Gain |
|---|------|-------|-----|---------------|
| O3 | `source_graph.py` | Every `connect()` runs schema migration + column checks | Guard with `PRAGMA user_version` check | ~0.5–1ms saved per connection |
| O4 | `source_graph.py` | `_build_index_locked()` loads all file/entity/edge rows into Python dicts | Use point queries or load only keys into a set | 30–50% memory reduction for large repos |
| O5 | `source_graph.py` | `_index_quality_scorecard` runs 8+ separate aggregation queries | Combine into conditional aggregation query | 6 fewer round-trips per build |
| O6 | `source_graph_ast.py` | `re.search` with inline regex patterns inside per-class loop | Compile regexes once at module level | ~10–20% speedup for PHP class extraction |
| O7 | `storage_observability.py` | `_tree_size` uses `os.walk` + `Path.stat()` — double stat per file | Use `os.scandir` which caches `is_dir()/is_file()` | ~2× faster tree size measurement |
| O8 | `task_engine.py` | Card modifications use full `json.loads` → modify → `json.dumps(sort_keys=True)` pattern | Use SQLite JSON functions (`json_set`) for simple field updates | Reduced CPU for card mutations |

### LOW IMPACT

| # | File | Issue | Fix |
|---|------|-------|-----|
| O9 | `context_cache.py` | Double JSON serialization in `key_material` + `cache_key_sha256` | Hash canonical JSON directly without round-trip |
| O10 | `context_graph.py` | FTS5 fallback LIKE query does full table scan | Add covering indexes or accept limitation |
| O11 | `source_graph.py` | `connect()` forces DELETE journal mode on every connection | Migrate only on write connections; accept WAL for reads |

---

## Part 3: Recommendations — Priority Order

### Immediate (low-risk, high-impact)

1. **SQLite WAL mode for task store** (O1) — One-time 5-line pragma change. Eliminates the most common runtime contention issue.

2. **Streaming string masking** (O2) — Replace `list(text)` in `_mask_php_non_code` and `_mask_c_family_non_code`. Major memory and speed win for large repositories.

3. **Fix `_atomic_write_json` fd leak** (M1) — One-line fix: close fd on `os.fdopen` failure.

### Short-term (next release cycle)

4. **Callback enqueue transaction atomicity** (H3) — Move callback enqueue into the same transaction as task state change.

5. **`enqueue_callback` error handling** (H2) — Add `finally: conn.rollback()` for non-IntegrityError exceptions.

6. **VS Code extension recovery circuit** (VH1, VH2) — Add episode counter and atomic state capture.

7. **Combined aggregation queries** (O5) — Single query for quality scorecard stats.

### Nice-to-have

8. **`except Exception` narrowing** (H1) — Replace bare `except Exception` with specific exception types.
9. **`sanitizeWebviewPayload` Windows paths** (VM2) — Add Windows absolute path pattern.
10. **`scandir` for tree size** (O7) — Minor perf improvement.
11. **Regex compilation at module level** (O6) — Small compile-time win.

---

## Part 4: Positive Findings

Areas where the codebase demonstrates excellent practices:

- **Defensive type checking** — `isinstance(value, bool)` guards correctly handle `bool` being a subclass of `int`
- **Optimistic locking** — Card-level compare-and-swap (`WHERE card_json=?`) prevents lost updates
- **Proper cleanup patterns** — `finally` blocks, VS Code disposable subscriptions, timer clearing in `deactivate()`
- **CSP security** — Webview uses strict nonce-based CSP, no `unsafe-inline`, `default-src 'none'`
- **Message validation** — Inbound webview messages validated against `ALLOWED_INBOUND_MESSAGE_TYPES` set
- **Atomic writes** — Consistent use of temp-file + `os.replace` pattern across the codebase
- **Path redaction** — Sensitive paths sanitized before posting to webview
- **Graceful degradation** — Features degrade rather than crash on unsupported platforms

---

*Report generated by 3 parallel Explore subagents. Each agent independently scanned its assigned scope for 30-60 seconds, producing structured findings aggregated here.*
