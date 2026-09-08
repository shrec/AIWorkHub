"""Operator recipe modules, runnable in any repository AIWorkHub manages.

Each module answers one question a manager otherwise answers by typing a fresh
Python heredoc: what happened to this card, what did this request log say, is
that ``processing`` pid actually alive, what does this candidate worktree
differ by. They are the executable half of ``manager_recipe_tools``'s
``aiworkhub.operator.*`` catalogue entries, and each is invoked as

    python -m aiworkhub.recipes.<name> --flag value

**Why a package and not a scripts directory.** These were checked in under
``scripts/recipes/`` and their manifests rendered ``python
scripts/recipes/<name>.py ...`` -- a repository-relative path that exists in
AIWorkHub's own checkout and nowhere else. In a managed project that file is
absent, so the recipe registered, validated, rendered a perfectly shaped argv
and could only ever fail at run time, while the per-project data it reads was
sitting there correct. A module path travels with the installed package, so the
same manifest is valid in every repository AIWorkHub manages.

Nothing here is imported by the rest of ``aiworkhub``: these modules are
spawned as subprocesses by ``recipe_runner``, never called in-process. The
package exists so the interpreter can find them by module path, and importing
it must therefore stay free -- no module is imported here, so ``import
aiworkhub.recipes`` costs nothing and pulls in no dependency.

The repository each module answers about is the working directory's, resolved
by :func:`_common.resolve_repo_root`; ``recipe_runner`` spawns every recipe
with ``cwd`` set to the target repository root.
"""

from __future__ import annotations

__all__: list[str] = []
