"""Exact registered manager tool names the host defers, as one ToolSearch select line.

The tuple is owned by the import-free tool_guard_hook (it is copied standalone to
.claude/hooks/); this leaf only re-exports it so manager_bootstrap and core share one object.
"""

from .tool_guard_hook import MANAGER_TOOLSEARCH_SELECT_NAMES

__all__ = ["MANAGER_TOOLSEARCH_SELECT_NAMES"]
