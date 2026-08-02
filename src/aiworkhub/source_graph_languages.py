"""Canonical Source Graph language registry.

The registry is intentionally dependency-free and truthful about capability:
Python has an AST extractor; PHP, C/C++/CUDA, JavaScript/TypeScript, Rust, Go,
Java and C# have conservative structural lexical extractors. Every other
registered language receives exact file-level evidence. Keeping discovery and capability in one table prevents the
historic bug where VS Code advertised a language but Source Graph silently
ignored its files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    id: str
    label: str
    extensions: tuple[str, ...]
    capability: str = "file_evidence"


# Exactly 34 repository language/file families.  Families keep the dashboard
# usable (for example one C/C++ switch) while covering the extensions users
# expect from a polyglot repository, including structured JSON/XML inputs and
# the Markdown documents that carry repository contracts and roadmaps.
LANGUAGE_SPECS: tuple[LanguageSpec, ...] = (
    LanguageSpec("python", "Python", (".py", ".pyi"), "semantic_ast"),
    LanguageSpec("php", "PHP", (".php", ".phtml", ".php3", ".php4", ".php5", ".php7", ".php8"), "semantic_lexical"),
    LanguageSpec("javascript", "JavaScript", (".js", ".jsx", ".mjs", ".cjs"), "semantic_lexical"),
    LanguageSpec("typescript", "TypeScript", (".ts", ".tsx", ".mts", ".cts"), "semantic_lexical"),
    LanguageSpec("cpp", "C / C++ / CUDA / OpenCL / Metal", (".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".ipp", ".inl", ".cu", ".cuh", ".cl", ".metal"), "semantic_lexical"),
    LanguageSpec("csharp", "C#", (".cs",), "semantic_lexical"),
    LanguageSpec("java", "Java", (".java",), "semantic_lexical"),
    LanguageSpec("kotlin", "Kotlin", (".kt", ".kts")),
    LanguageSpec("scala", "Scala", (".scala", ".sc")),
    LanguageSpec("go", "Go", (".go",), "semantic_lexical"),
    LanguageSpec("rust", "Rust", (".rs",), "semantic_lexical"),
    LanguageSpec("swift", "Swift", (".swift",)),
    LanguageSpec("objective_c", "Objective-C", (".m", ".mm")),
    LanguageSpec("ruby", "Ruby", (".rb", ".rake", ".gemspec")),
    LanguageSpec("perl", "Perl", (".pl", ".pm", ".t")),
    LanguageSpec("lua", "Lua", (".lua",)),
    LanguageSpec("r", "R", (".r", ".rmd")),
    LanguageSpec("julia", "Julia", (".jl",)),
    LanguageSpec("dart", "Dart", (".dart",)),
    LanguageSpec("elixir", "Elixir", (".ex", ".exs")),
    LanguageSpec("erlang", "Erlang", (".erl", ".hrl")),
    LanguageSpec("haskell", "Haskell", (".hs", ".lhs")),
    LanguageSpec("clojure", "Clojure", (".clj", ".cljs", ".cljc", ".edn")),
    LanguageSpec("fsharp", "F#", (".fs", ".fsi", ".fsx")),
    LanguageSpec("visual_basic", "Visual Basic", (".vb",)),
    LanguageSpec("shell", "Shell", (".sh", ".bash", ".zsh", ".fish")),
    LanguageSpec("powershell", "PowerShell", (".ps1", ".psm1", ".psd1")),
    LanguageSpec("sql", "SQL", (".sql",)),
    LanguageSpec(
        "json",
        "JSON / JSON Lines",
        (".json", ".jsonc", ".json5", ".geojson", ".jsonl", ".ndjson"),
    ),
    LanguageSpec("yaml", "YAML", (".yaml", ".yml")),
    LanguageSpec("toml", "TOML", (".toml",)),
    LanguageSpec("xml", "XML", (".xml", ".xsd", ".xsl", ".xslt", ".svg")),
    LanguageSpec("web", "Web markup / styles", (".html", ".htm", ".css", ".scss", ".sass", ".less", ".vue", ".svelte")),
    LanguageSpec("documentation", "Markdown documentation", (".md", ".markdown", ".mdx")),
)

LANGUAGE_BY_ID: dict[str, LanguageSpec] = {spec.id: spec for spec in LANGUAGE_SPECS}
LANGUAGE_BY_EXTENSION: dict[str, str] = {
    extension: spec.id
    for spec in LANGUAGE_SPECS
    for extension in spec.extensions
}
INDEXED_EXTENSIONS: tuple[str, ...] = tuple(sorted(LANGUAGE_BY_EXTENSION))
LANGUAGE_CAPABILITIES: dict[str, str] = {
    spec.id: spec.capability for spec in LANGUAGE_SPECS
}

if len(LANGUAGE_BY_ID) != 34:  # pragma: no cover - import-time invariant
    raise RuntimeError("source_graph_language_registry_must_have_34_families")
if len(LANGUAGE_BY_EXTENSION) != sum(len(spec.extensions) for spec in LANGUAGE_SPECS):
    raise RuntimeError("source_graph_language_extension_collision")


def language_for_path(path: Path) -> str | None:
    return LANGUAGE_BY_EXTENSION.get(path.suffix.lower())


def public_registry(*, disabled_languages: frozenset[str] = frozenset()) -> list[dict[str, object]]:
    return [
        {
            "id": spec.id,
            "label": spec.label,
            "extensions": list(spec.extensions),
            "capability": spec.capability,
            "enabled": spec.id not in disabled_languages,
        }
        for spec in LANGUAGE_SPECS
    ]


__all__ = [
    "INDEXED_EXTENSIONS",
    "LANGUAGE_BY_EXTENSION",
    "LANGUAGE_BY_ID",
    "LANGUAGE_CAPABILITIES",
    "LANGUAGE_SPECS",
    "LanguageSpec",
    "language_for_path",
    "public_registry",
]
