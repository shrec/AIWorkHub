# GeoAI Task MCP Agent Notes

This repository is intended to become an independent submodule.

Rules:
- Keep the task queue source of truth in the parent GeoAI repository.
- Do not duplicate `AITools/taskctl.py` logic here unless a deliberate migration task says so.
- Prefer wrapping `taskctl.py` through stable command boundaries.
- Write operations must remain explicit and gated by `GEOAI_TASK_MCP_ALLOW_WRITES=1`.
- Never run `git add -A` from this repository against the parent repository.

When printing worker auto-pickup or exact claim-start cards, use one
self-contained fenced block per model. The instruction below must be the final
non-empty line inside the same copyable block, never adjacent prose:

```text
გაუშვი პარალელური სუბაგენტები და რომ დასრულდება დაუბრუნე კოდექსს რევიუსთვის
```
