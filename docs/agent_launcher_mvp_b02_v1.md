# Agent Launcher MVP B02

This is a contract-only MVP for future Codex-managed worker launching.

## Scope

- Defines request/response schemas for `launch_task`, `task_status`, `collect_result`, and `cancel_task`.
- Preserves `AITools/taskctl.py` JSONL/SQLite as the source of truth.
- Runs only as a dry-run schema and simulation contract.
- Adds no subprocess, shell, network, paid CLI, or live agent launch path.

## Safety Rules

- `launch_implemented=false`
- `launch_enabled_by_default=false`
- `shell_execution_used=false`
- `network_launch_used=false`
- `real_agent_spawn_used=false`
- `review_ready=false` from MCP collection; workers must still return through `taskctl review`.

## Future Gate

Real launch support requires a separate gated executor task with:

- explicit owner approval,
- runner/topic allowlist,
- command allowlist,
- collision guard preflight,
- audit-log append,
- token/cost reporting,
- dry-run default,
- rollback/cancel contract.

This task intentionally stops before execution.
