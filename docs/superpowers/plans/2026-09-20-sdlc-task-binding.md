# Repository-bound SDLC Task Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve one canonical task to at most one SDLC case without inventing Plan/Design evidence or changing the task queue's authority.

**Architecture:** Extend the existing case store with an exact repository-scoped task link and a bounded lookup. A manager MCP wrapper creates a case for an existing canonical card under the same verified write gate as Task creation. This is an opt-in binding foundation; launch gating and worker-stage delivery are separate follow-ons and must not be claimed here.

**Tech Stack:** Python 3.12+, SQLite, current `sdlc_case_store`, `task_store`, FastMCP, pytest.

**Spec:** `docs/superpowers/specs/2026-09-20-model-neutral-sdlc-design.md`

## Review Focus

1. An attempted link to a foreign or nonexistent task refuses before any case mutation.
2. Two case IDs cannot claim the same `(repo_id, task_id)`; an exact request replay returns the same receipt, while a different request is a conflict.
3. Existing cases without task links remain readable and retain their original digest; migration never synthesizes stage receipts.
4. Unknown, stale, or ambiguous task binding remains `UNKNOWN`, not Plan/Design ready.
5. Write gate off or unverified manager leaves the SQLite bytes unchanged.

### Task 1: Exact task link in the case store

**Files:** `src/aiworkhub/sdlc_case_store.py`, `tests/test_sdlc_case_store.py`

- [ ] Add failing tests for exact task-link lookup, duplicate binding, replay, cross-repo link, and legacy case rows. Use two bootstrapped temporary repositories rather than a mocked foreign repo ID alone.
- [ ] Run `python3 -m pytest -q tests/test_sdlc_case_store.py` and observe the new tests fail for the absent interface.
- [ ] Implement a bounded `case_for_task(repo_root, repo_id, task_id)` read and a unique binding recorded atomically with `create_case(..., links={"task_id": ...})`. Verify the task exists in the same canonical `task_store` before insertion. Preserve the existing `links_json` and case digest; use a compatible migration only if an index/table is needed. Return a typed unknown for no link and refuse ambiguous or conflicting links. Do not fabricate any stage receipt.
- [ ] Run `python3 -m pytest -q tests/test_sdlc_case_store.py tests/test_sdlc_case_mcp.py`, `python3 -m ruff check src/aiworkhub/sdlc_case_store.py tests/test_sdlc_case_store.py`, and `git diff --check`.
- [ ] Stop at Codex review. Only after acceptance, commit the exact two paths.

### Task 2: Manager MCP create/read by exact task

**Depends on:** Task 1 accepted.

**Files:** `src/aiworkhub/core.py`, `src/aiworkhub/server.py`, `tests/test_sdlc_case_mcp.py`

- [ ] Add failing tests for `aiworkhub_manager_sdlc_case_create_for_task(task_id, request_id)` and `aiworkhub_manager_sdlc_case_for_task(task_id)`. The public tool cannot accept `repo_id`, arbitrary links, or caller-supplied task authority. Unknown/foreign task and gate-off cases leave storage absent or byte-identical.
- [ ] Run `python3 -m pytest -q tests/test_sdlc_case_mcp.py` red.
- [ ] Reuse the verified manager identity, `AIWORKHUB_ALLOW_WRITES=1`, canonical task-store readiness, and the Task 1 store API. A successful create returns a deterministic case ID and receipt; read returns the exact bounded case/stage packet or typed unknown. No provider-specific stage semantics or background writes.
- [ ] Run `python3 -m pytest -q tests/test_sdlc_case_mcp.py tests/test_sdlc_case_store.py tests/test_server.py`, `python3 -m ruff check src/aiworkhub/core.py src/aiworkhub/server.py tests/test_sdlc_case_mcp.py`, and `git diff --check`.
- [ ] Stop at Codex review. Only after acceptance, commit the exact three paths.

## Follow-on boundary

Worker stage packets, claim/launch gates, accepted Test receipts, release/deploy policy, Maintain outcome metrics, and UI projection require separate reviewed work. Until those are accepted and measured, this binding is not full Playbook enforcement. Legacy cards are not automatically assigned fictitious Plan or Design decisions.
