"""Validation executables for every Source Graph language family.

A card must be runnable for every language Source Graph indexes, so each family
id in ``source_graph_languages.LANGUAGE_BY_ID`` maps to the executables that
validate it.  This module is data only and dependency-free: ``worker_workspace``
imports :data:`SYSTEM_VALIDATION_EXECUTABLES` as its trusted host-tool set, and
every member takes the one git/node/cmake trust path there unchanged --
``shutil.which`` resolution, rejection when it resolves inside the repository
or a worktree, the same argv normalization and recorded ``--version`` fact.

Whether the Windows AppContainer can reach these toolchains is not measured
here; trust only decides which heads may be resolved, never that they exist.
"""

from __future__ import annotations

# pytest, ruff and mypy resolve through the trusted runtime-root authority
# (the repository .venv), never through PATH, so they are not system tools.
PYTHON_VALIDATION_EXECUTABLES = frozenset({"pytest", "ruff", "mypy"})
# node, npm and npx are one trusted system-tool family: an nvm install places
# all three beneath the same ``versions/node/vX.Y.Z/bin`` root, and a card
# that runs ``npm --prefix <dir> test`` is exactly as launch-capable as one
# that runs ``node`` directly once that family is trusted (NF-2026-00625 M2).
NODE_FAMILY_SYSTEM_EXECUTABLES = frozenset({"node", "npm", "npx"})
# cmake and ctest are the same kind of family: one installer places both in
# one ``bin`` directory, and a card gating on ``cmake --build`` + ``ctest`` was
# refused as ``executable:cmake`` on a host where both are installed.  cpack is
# left out: it packages rather than validates, and the bare name can resolve to
# Chocolatey's legacy ``cpack`` shim instead.
CMAKE_FAMILY_SYSTEM_EXECUTABLES = frozenset({"cmake", "ctest"})
_NATIVE = CMAKE_FAMILY_SYSTEM_EXECUTABLES | {"clang", "gcc", "make"}
_DOTNET = frozenset({"dotnet"})

VALIDATION_EXECUTABLES_BY_LANGUAGE: dict[str, frozenset[str]] = {
    "python": PYTHON_VALIDATION_EXECUTABLES,
    "php": frozenset({"php", "phpunit", "composer"}),
    "javascript": NODE_FAMILY_SYSTEM_EXECUTABLES,
    "typescript": NODE_FAMILY_SYSTEM_EXECUTABLES,
    "cpp": _NATIVE,
    "csharp": _DOTNET,
    "java": frozenset({"java", "javac", "mvn", "gradle"}),
    "kotlin": frozenset({"kotlinc", "gradle"}),
    "scala": frozenset({"sbt", "scala"}),
    "go": frozenset({"go"}),
    "rust": frozenset({"cargo", "rustc"}),
    "swift": frozenset({"swift"}),
    "objective_c": _NATIVE,
    "ruby": frozenset({"ruby", "bundle", "rspec", "rake"}),
    "perl": frozenset({"perl", "prove"}),
    "lua": frozenset({"lua"}),
    "r": frozenset({"Rscript"}),
    "julia": frozenset({"julia"}),
    "dart": frozenset({"dart", "flutter"}),
    "elixir": frozenset({"mix", "elixir"}),
    "erlang": frozenset({"erl", "rebar3"}),
    "haskell": frozenset({"ghc", "cabal", "stack"}),
    "clojure": frozenset({"clojure", "lein"}),
    "fsharp": _DOTNET,
    "visual_basic": _DOTNET,
    "shell": frozenset({"bash"}),
    "powershell": frozenset({"pwsh"}),
    "sql": frozenset({"sqlite3"}),
    "json": PYTHON_VALIDATION_EXECUTABLES,
    "yaml": PYTHON_VALIDATION_EXECUTABLES,
    "toml": PYTHON_VALIDATION_EXECUTABLES,
    "xml": PYTHON_VALIDATION_EXECUTABLES,
    "web": NODE_FAMILY_SYSTEM_EXECUTABLES,
    "documentation": PYTHON_VALIDATION_EXECUTABLES,
}

# Every host tool trusted through ``shutil.which``: git plus each language
# family's executables, minus the runtime-root Python validators.
SYSTEM_VALIDATION_EXECUTABLES = frozenset({"git"}).union(
    *VALIDATION_EXECUTABLES_BY_LANGUAGE.values()
) - PYTHON_VALIDATION_EXECUTABLES
