"""Claude Code PreToolUse guard routing raw discovery, reads and edits to AIWorkHub tools.

This module is standalone and stdlib-only on purpose: ``provider_tool_guards``
copies it byte-for-byte to ``.claude/hooks/aiworkhub_tool_guard.py`` and the
managed hook runs that copy with whatever ``python`` the session resolves, so it
can import nothing from ``aiworkhub``.

A PreToolUse hook that exits 2 blocks the tool call, so this script never does:
the only way it refuses anything is a positive rule match, for which it prints
exactly one ``permissionDecision: deny`` object and exits 0. Allow, malformed
input and any exception all mean no output and exit 0.

Rules, first match wins:

* R1 raw search (grep, rg, findstr, Select-String, git grep) of indexed source.
  A searcher with no file operand that reads a pipe or stdin searches no tree.
* R2 recursive listing (find, fd, tree, ls -R, Get-ChildItem -Recurse, dir /s)
  of an indexed directory.
* R3 raw read (cat, head, sed, Get-Content, ...) of an indexed file.
* R4 inline python/node code that reads an indexed file or lists an indexed
  directory.
* R5 Read of an indexed file, whatever its limit or offset.
* R6 Edit of an existing repository file.
* R7 Write over an existing repository file; Write only creates new files.
* R8 Read, Edit, or Write over an existing file, of any path inside the
  repo-local scratch root ``.scratch/``; checked before R5-R7. Write that
  creates a new scratch file is allowed, as R7 allows it elsewhere. A Read of
  a real PNG/JPEG/WEBP (suffix and magic bytes) under ``.scratch/<session_id>/``
  of the calling session is also allowed, and so is a Read of a scratch file
  the Source Graph policy excludes (``.scratch/<session_id>/<dir>/...``, which
  Source Graph answers with excluded_by_policy and no preview). Edit, Write
  over an existing file, and a Read of the session root's text or of another
  session's non-excluded files stay denied.

"Indexed" is Source Graph's own membership when its canonical database
(``.aiworkhub/source_graph/source_graph.sqlite``) holds a readable generation:
a path that generation lists as not indexed, or omits, is unindexed. Without
one the repository ``IndexPolicy`` (suffix, excludes, include roots) decides.
A path that resolves outside the repository root is never an R1-R7 target.

R6 and R7 leave ``.git`` and ``.aiworkhub`` alone: VCS and runtime state is not
source the semantic editor can take over.

``--stop`` is the direct-chat manager's Stop hook (NF-2026-02110). It reads the
task queue read-only and blocks the stop -- one ``{"decision": "block"}``
object, exit 0 -- while a card this session dispatched (its
``origin_thread_id`` is the hook's ``session_id``) awaits a review decision, or
is processing with no live ``manager_callback_watch`` heartbeat for the
session. It fails open: ``stop_hook_active``, a missing or foreign database and
any error all allow the stop. ``--reminder`` prints the post-compaction
reminder.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REASON_MAX_CHARS = 400
REMINDER_MAX_BYTES = 1200
# Exact registered manager tool names the host defers; manager_bootstrap hands
# them back as one ToolSearch select line, which this hook prints with --reminder.
MANAGER_TOOLSEARCH_SELECT_NAMES: tuple[str, ...] = (
    "aiworkhub_task_show",
    "aiworkhub_agent_task_status",
    "aiworkhub_review_packet",
    "aiworkhub_agent_accept_preview",
    "aiworkhub_agent_accept_review",
    "aiworkhub_task_reject_review",
    "needfix_show",
    "needfix_list",
    "needfix_preview_convert",
    "needfix_convert",
    "aiworkhub_agent_launch_task",
    "aiworkhub_claude_callback_wait",
    "aiworkhub_claude_callback_ack",
    "aiworkhub_manager_source_graph_query",
    "aiworkhub_manager_workforce_rank",
)
# Source Graph's non-bypassable directory excludes; ``source_graph`` imports
# them as ``DEFAULT_EXCLUDE_DIR_NAMES``. This module owns them because it can
# import nothing from ``aiworkhub``.
EXCLUDED_DIRS = frozenset({
    ".git", "__pycache__", ".venv", "venv", "env", "node_modules",
    ".aiworkhub", ".mypy_cache", ".pytest_cache", ".tox", ".ruff_cache",
    "dist", "build", "archive", "logs", ".tmp",
    # CMake writes non-source ``.ts`` timestamp/dependency-tracking files
    # here (e.g. ``compiler_depend.ts``) -- indexing them as file-level
    # "typescript" evidence would be a false language label, not truthful
    # evidence of real JS/TS source.
    "CMakeFiles",
    # Ephemeral nested worktrees and editor/agent state, not canonical
    # source: ``.claude/worktrees/<task>`` holds full nested checkouts of
    # this same repository (their own ``tools/``, ``scripts/`` trees would
    # otherwise be indexed a second time per active worktree).
    ".claude", ".hg", ".svn", ".cache",
})
# New repository policy files start with only high-confidence generated
# measurement artifacts excluded.  JSON/XML remain enabled languages and
# ordinary configuration/data files remain indexable; owners can remove any
# of these editable globs from ``.aiworkhub/config/source_graph.json``.
# ``source_graph`` imports them as ``DEFAULT_CONFIG_EXCLUDE_GLOBS``.
DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = (
    "eval/*.json",
    "eval/**/*.json",
    "eval/*.jsonl",
    "eval/**/*.jsonl",
)
_POLICY_PARTS = (".aiworkhub", "config", "source_graph.json")
_POLICY_MAX_BYTES = 64 * 1024
_WORKTREE_MIRROR_HEAD = (".aiworkhub", "runtime", "worktrees")
_WORKTREE_ID = re.compile(r"[0-9a-f]{32}")
# Language id -> extensions, mirroring ``source_graph_languages.LANGUAGE_SPECS``
# (a test asserts equality). Source Graph indexes the extensions of every
# language the repository policy does not list in ``disabled_languages``.
LANGUAGE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "python": (".py", ".pyi"),
    "php": (".php", ".phtml", ".php3", ".php4", ".php5", ".php7", ".php8"),
    "javascript": (".js", ".jsx", ".mjs", ".cjs"),
    "typescript": (".ts", ".tsx", ".mts", ".cts"),
    "cpp": (
        ".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".ipp", ".inl", ".inc",
        ".cu", ".cuh", ".cl", ".metal",
    ),
    "csharp": (".cs",),
    "java": (".java",),
    "kotlin": (".kt", ".kts"),
    "scala": (".scala", ".sc"),
    "go": (".go",),
    "rust": (".rs",),
    "swift": (".swift",),
    "objective_c": (".m", ".mm"),
    "ruby": (".rb", ".rake", ".gemspec"),
    "perl": (".pl", ".pm", ".t"),
    "lua": (".lua",),
    "r": (".r", ".rmd"),
    "julia": (".jl",),
    "dart": (".dart",),
    "elixir": (".ex", ".exs"),
    "erlang": (".erl", ".hrl"),
    "haskell": (".hs", ".lhs"),
    "clojure": (".clj", ".cljs", ".cljc", ".edn"),
    "fsharp": (".fs", ".fsi", ".fsx"),
    "visual_basic": (".vb",),
    "shell": (".sh", ".bash", ".zsh", ".fish"),
    "powershell": (".ps1", ".psm1", ".psd1"),
    "sql": (".sql",),
    "json": (".json", ".jsonc", ".json5", ".geojson", ".jsonl", ".ndjson"),
    "yaml": (".yaml", ".yml"),
    "toml": (".toml",),
    "xml": (
        ".xml", ".xsd", ".xsl", ".xslt", ".svg", ".vcxproj", ".filters", ".props",
        ".targets", ".csproj", ".vbproj", ".fsproj",
    ),
    "web": (".html", ".htm", ".css", ".scss", ".sass", ".less", ".vue", ".svelte"),
    "documentation": (".md", ".markdown", ".mdx"),
}
INDEXED_SUFFIXES = frozenset(
    extension for extensions in LANGUAGE_EXTENSIONS.values() for extension in extensions
)
_EDIT_EXEMPT_DIRS = frozenset({".git", ".aiworkhub"})
# The one repo-local scratch root: seats keep scratch in ``.scratch/<session_id>/``.
# It is gitignored and a Source Graph include root, and ``storage_retention``
# prunes aged session dirs under it (NF-2026-01928).
SCRATCH_DIR = ".scratch"
# The manager's script exists only in the AIWorkHub repository itself; a deny
# reason names it only when the guarded root carries it (NF-2026-02088).
_MANAGER_SCRIPT_PARTS = ("scripts", "manager_semantic_edit.py")
# Source Graph's canonical database (``storage_registry``: source_graph) and
# the file-row statuses that list a path without indexing it. Any other status
# is a member: an unknown status keeps the path guarded.
SOURCE_GRAPH_DB_PARTS = (".aiworkhub", "source_graph", "source_graph.sqlite")
UNINDEXED_STATUSES = ("unindexed", "declared_input_unindexed", "skipped", "excluded")
_SOURCE_GRAPH_BUSY_SECONDS = 0.25

_DENIALS_PARTS = (".aiworkhub", "runtime", "tool_guard", "denials.jsonl")
_DENIALS_ROTATE_BYTES = 4 * 1024 * 1024
_STDIN_MAX_BYTES = 32 * 1024 * 1024
_COMMAND_MAX_CHARS = 1024 * 1024
_LITERALS_MAX = 256
_NESTING_MAX = 3

_SOURCE_GRAPH = "aiworkhub_manager_source_graph_query / aiworkhub_worker_source_graph_query"
_UNINDEXED = " A bounded Read is allowed when Source Graph reports the target unindexed."
_READ_INSTEAD = (
    "Use " + _SOURCE_GRAPH + ' mode="body" for a symbol or mode="file" target=<exact path>'
    ' query="START-END" for an exact line window; bodygrep gives exact line numbers.'
)
_EDIT_INSTEAD = (
    "Use aiworkhub_worker_semantic_edit_prepare then aiworkhub_worker_semantic_edit_apply"
    " (worker) or scripts/manager_semantic_edit.py --path {target} --start N --end M"
    " (manager)."
)
_REASONS = {
    "R1": (
        "AIWorkHub tool guard R1: raw search over indexed source ({target}) is denied. Use "
        + _SOURCE_GRAPH
        + ' mode="bodygrep" query={pattern}.'
    ),
    "R2": (
        "AIWorkHub tool guard R2: recursive listing of indexed directory {target} is"
        " denied. Use " + _SOURCE_GRAPH + ' mode="slice".'
    ),
    "R3": (
        "AIWorkHub tool guard R3: raw read of indexed file {target} is denied. "
        + _READ_INSTEAD
    ),
    "R4": (
        "AIWorkHub tool guard R4: inline code over indexed source {target} is denied. "
        + _READ_INSTEAD
    ),
    "R5": "AIWorkHub tool guard R5: Read of indexed file {target} is denied. " + _READ_INSTEAD,
    "R6": (
        "AIWorkHub tool guard R6: raw edit of an existing repository file is denied. "
        + _EDIT_INSTEAD
    ),
    "R7": (
        "AIWorkHub tool guard R7: Write over existing repository file {target} is denied;"
        " Write only creates new files. " + _EDIT_INSTEAD
    ),
    # ``{script}`` is filled by ``_reason``: the manager script only where it exists.
    "R8": (
        "AIWorkHub tool guard R8: Read (unless policy-excluded), Edit or overwrite"
        " of scratch file {target}"
        " is denied; Write may only create one. Read: " + _SOURCE_GRAPH
        + ' mode="body"/"file"/"bodygrep". Edit: aiworkhub_worker_semantic_edit_prepare/_apply'
        " or aiworkhub_manager_semantic_edit_prepare/_apply{script}."
    ),
}
REMINDER = (
    "AIWorkHub tool guard (PreToolUse hook) is active for indexed source:\n"
    "- Search: aiworkhub_manager_source_graph_query or aiworkhub_worker_source_graph_query"
    ' mode="bodygrep", not grep/rg/findstr/Select-String/git grep.\n'
    '- Listing: mode="slice", not find/fd/tree/ls -R/Get-ChildItem -Recurse/dir /s.\n'
    '- Reading: mode="body" for a symbol or mode="file" query="START-END" for a line'
    " window; built-in Read is denied on indexed files; not cat/head/sed/Get-Content or"
    " python/node file reads.\n"
    "- Editing an existing file: aiworkhub_worker_semantic_edit_prepare then"
    " aiworkhub_worker_semantic_edit_apply (worker) or scripts/manager_semantic_edit.py"
    " --path --start --end (manager); Write only creates new files.\n"
    "- A bounded Read is allowed when Source Graph reports the target unindexed."
)

_GLOB = re.compile(r"[*?\[]")
_SUFFIX = re.compile(r"\.([A-Za-z0-9]+)\Z")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_PS_ASSIGNMENT = re.compile(r"[-+*/%]?=")
_VARIABLE = re.compile(r"\$\{?(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}?", re.IGNORECASE)
_INTERPRETER = re.compile(r"python(?:\d+(?:\.\d+)*)?w?|py|pypy\d*(?:\.\d+)?|node(?:js)?")
_READ_CALL = re.compile(
    r"\bopen\(|read_text\(|read_bytes\(|readFileSync\(|readFile\(|readlines\("
)
_LIST_CALL = re.compile(r"\bos\.walk\(|glob\(|listdir\(|scandir\(|readdir(?:Sync)?\(|iterdir\(")
_LITERAL = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"|`([^`\n]*)`")
_CMD_SWITCH = re.compile(r"(?:/[A-Za-z?](?::\S*)?)+")
_FINDSTR_OPTION = re.compile(r"[/-]([A-Za-z]+)(?::(.*))?", re.DOTALL)
_MSYS_DRIVE = re.compile(r"/(?:cygdrive/)?([A-Za-z])(?=/|\Z)(.*)", re.DOTALL)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

_SEARCHERS = frozenset(
    {"grep", "egrep", "fgrep", "rg", "ag", "ack", "ack-grep", "findstr", "select-string", "sls"}
)
_LISTERS = frozenset({"find", "fd", "fdfind", "tree", "ls", "dir", "get-childitem", "gci"})
_READERS = frozenset(
    {
        "cat", "type", "get-content", "gc", "head", "tail", "sed", "awk", "gawk", "mawk",
        "nawk", "less", "more", "nl", "bat", "batcat",
    }
)
_DIRECTORY_CHANGERS = frozenset({"cd", "chdir", "pushd", "set-location", "sl", "push-location"})
_POSIX_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_KEYWORDS = frozenset({"!", "{", "}", "do", "then", "else", "elif", "if", "while", "until", "time"})
_WRAPPERS = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U"}),
    "nohup": frozenset(),
    "command": frozenset(),
    "builtin": frozenset(),
    "exec": frozenset({"-a"}),
    "nice": frozenset({"-n"}),
    "env": frozenset({"-u", "-C", "-S"}),
    "stdbuf": frozenset(),
    "timeout": frozenset({"-s", "-k"}),
    "xargs": frozenset({"-I", "-n", "-P", "-L", "-d", "-s", "-E", "-a"}),
}
_GREP_LONG = frozenset(
    {
        "regexp", "file", "max-count", "after-context", "before-context", "context",
        "directories", "devices", "include", "exclude", "exclude-dir", "exclude-from",
        "label", "binary-files",
    }
)
_RG_SHORT = "ABCEMTdefgjmrt"
_RG_LONG = frozenset(
    {
        "regexp", "file", "glob", "iglob", "type", "type-not", "type-add", "type-clear",
        "max-count", "after-context", "before-context", "context", "max-columns",
        "threads", "max-depth", "max-filesize", "encoding", "replace", "color", "colors",
        "context-separator", "field-context-separator", "field-match-separator",
        "path-separator", "pre", "pre-glob", "sort", "sortr", "ignore-file",
        "dfa-size-limit", "regex-size-limit", "engine", "hyperlink-format", "generate",
    }
)
_AG_LONG = frozenset(
    {
        "after", "before", "context", "file-search-regex", "ignore", "ignore-dir",
        "max-count", "depth", "path-to-ignore", "pager", "width", "type", "match",
        "output", "ignore-file",
    }
)
_GIT_GLOBAL_VALUED = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
)
_GIT_GREP_LONG = frozenset(
    {
        "regexp", "file", "max-depth", "threads", "max-count", "after-context",
        "before-context", "context",
    }
)
_LS_LONG = frozenset(
    {
        "ignore", "hide", "block-size", "format", "indicator-style", "quoting-style",
        "sort", "time", "time-style", "tabsize", "width",
    }
)
_FD_LONG = frozenset(
    {
        "max-depth", "min-depth", "exact-depth", "exclude", "type", "extension", "size",
        "threads", "color", "owner", "changed-within", "changed-before",
        "change-newer-than", "change-older-than", "newer", "older", "search-path",
        "base-directory", "path-separator", "max-results", "ignore-file", "format",
        "batch-size", "and",
    }
)
_TREE_LONG = frozenset(
    {"charset", "filelimit", "timefmt", "sort", "gitfile", "infofile", "hintro", "houtro"}
)
_HEAD_LONG = frozenset({"lines", "bytes", "sleep-interval", "pid", "max-unchanged-stats"})
_SED_LONG = frozenset({"expression", "file", "line-length"})
_AWK_LONG = frozenset({"field-separator", "assign", "file", "include", "load", "exec", "source"})
_NL_LONG = frozenset(
    {
        "body-numbering", "section-delimiter", "footer-numbering", "header-numbering",
        "line-increment", "join-blank-lines", "number-format", "number-separator",
        "starting-line-number", "number-width",
    }
)
_BAT_LONG = frozenset(
    {
        "language", "highlight-line", "map-syntax", "line-range", "tabs", "wrap",
        "terminal-width", "color", "italic-text", "decorations", "paging", "pager",
        "theme", "style", "file-name", "ignored-suffix", "diff-context",
        "squeeze-limit", "nonprintable-notation", "binary",
    }
)
_NODE_VALUED = frozenset(
    {
        "-r", "--require", "--import", "--loader", "--experimental-loader", "-C",
        "--conditions", "--input-type", "--env-file", "--title", "--inspect-port",
    }
)
_PWSH_VALUED = frozenset(
    {
        "executionpolicy", "ep", "ex", "exec", "windowstyle", "w", "win", "inputformat",
        "if", "inp", "outputformat", "of", "o", "out", "workingdirectory", "wd",
        "configurationname", "config", "version", "v", "settingsfile", "psconsolefile",
    }
)
_PS_COMMON = (
    "erroraction", "warningaction", "informationaction", "errorvariable",
    "warningvariable", "informationvariable", "outvariable", "outbuffer",
    "pipelinevariable",
)
_PS_ALIASES = {
    "ea": "erroraction", "wa": "warningaction", "infa": "informationaction",
    "ev": "errorvariable", "wv": "warningvariable", "iv": "informationvariable",
    "ov": "outvariable", "ob": "outbuffer", "pv": "pipelinevariable",
    "lp": "literalpath", "pspath": "literalpath",
}
_SLS_PARAMS = (
    "pattern", "path", "literalpath", "inputobject", "encoding", "context", "include",
    "exclude", "culture",
) + _PS_COMMON
_SLS_SWITCHES = (
    "simplematch", "casesensitive", "notmatch", "allmatches", "raw", "quiet", "list",
    "noemphasis",
)
_GCI_PARAMS = (
    "path", "literalpath", "filter", "include", "exclude", "depth", "attributes",
) + _PS_COMMON
_GCI_SWITCHES = (
    "recurse", "name", "force", "file", "directory", "hidden", "readonly", "system",
    "followsymlink",
)
_GCI_ALIASES = dict(
    _PS_ALIASES,
    s="recurse", ad="directory", d="directory", af="file", ah="hidden", h="hidden",
    ar="readonly",
    **{"as": "system"},
)
_GC_PARAMS = (
    "path", "literalpath", "totalcount", "head", "first", "tail", "last", "readcount",
    "encoding", "delimiter", "filter", "include", "exclude", "stream",
) + _PS_COMMON
_GC_SWITCHES = ("raw", "wait", "force", "asbytestream")


class _Segment:
    """One simple command: its argv words plus what feeds its stdin."""

    __slots__ = ("words", "piped", "stdin", "inputs", "redirected")

    def __init__(self, piped: bool) -> None:
        self.words: list[str] = []
        self.piped = piped
        self.stdin: str | None = None
        self.inputs: list[str] = []
        # Every "< file" operand, also kept in ``inputs``: stdin, never a FILE
        # argument. "grep -r x < a < b" redirects twice; neither is a FILE.
        self.redirected: list[str] = []

    def text(self) -> str:
        return " ".join(self.words) + "\n" + (self.stdin or "")


def _closing_paren(text: str, open_at: int) -> int:
    depth = 0
    quote = ""
    at = open_at
    while at < len(text):
        char = text[at]
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return at
        at += 1
    return len(text)


class _Lexer:
    """Split one command line into simple-command segments.

    Not a shell: it recovers argv words, pipes, redirections, heredoc bodies and
    command substitutions well enough to classify each simple command. The
    dialect picks the quoting rules: ``bash``, ``pwsh`` or ``cmd``.
    """

    def __init__(self, text: str, dialect: str) -> None:
        self.text = text
        self.end = len(text)
        self.at = 0
        self.dialect = dialect
        self.escape = {"bash": "\\", "pwsh": "`"}.get(dialect, "^")
        self.segments: list[_Segment] = []
        self.nested: list[str] = []
        self.heredocs: list[tuple[str, bool, _Segment]] = []
        self.segment = _Segment(False)
        self.redirect: str | None = None
        self.chars: list[str] = []
        self.started = False

    def run(self) -> tuple[list[_Segment], list[str]]:
        while self.at < self.end:
            self._step()
        self._close(False)
        return self.segments, self.nested

    def _step(self) -> None:
        text, at, dialect = self.text, self.at, self.dialect
        char = text[at]
        after = text[at + 1] if at + 1 < self.end else ""
        if char == self.escape:
            if after == "\n":
                self.at = at + 2
            elif after == "\r" and text.startswith("\n", at + 2):
                self.at = at + 3
            else:
                if after:
                    self._take(after)
                self.at = at + 2
        elif char in " \t\r":
            self._flush()
            self.at = at + 1
        elif char == "\n":
            self._close(False)
            self.at = at + 1
            if self.heredocs:
                self._heredoc_bodies()
        elif char == "#" and not self.started and dialect != "cmd":
            newline = text.find("\n", at)
            self.at = self.end if newline < 0 else newline
        elif char == "'" and dialect != "cmd":
            self._single_quoted()
        elif char == '"':
            self._double_quoted()
        elif (
            char == "@"
            and dialect == "pwsh"
            and not self.started
            and after in ("'", '"')
            and self._here_string()
        ):
            pass
        elif char == "@" and dialect == "pwsh" and not self.started and after in ("(", "{"):
            self.at = at + 1
        elif char == "$" and after == "'" and dialect == "bash":
            self._ansi_c_quoted()
        elif char == "$" and after == "{" and dialect != "cmd":
            close = text.find("}", at + 2)
            close = self.end - 1 if close < 0 else close
            self._take(text[at:close + 1])
            self.at = close + 1
        elif char == "$" and after == "(" and dialect != "cmd":
            self._close(False)
            self.at = at + 2
        elif char == "`" and dialect == "bash":
            close = text.find("`", at + 1)
            close = self.end if close < 0 else close
            self.nested.append(text[at + 1:close])
            self.started = True
            self.at = close + 1
        elif char == "|":
            if after == "|":
                self._close(False)
                self.at = at + 2
            else:
                self._close(True)
                self.at = at + (2 if after == "&" and dialect == "bash" else 1)
        elif char == "&":
            self._ampersand(after)
        elif char in ";()" or (char in "{}" and dialect == "pwsh"):
            self._close(False)
            self.at = at + 1
        elif char in "<>":
            self._redirection()
        else:
            self._take(char)
            self.at = at + 1

    def _take(self, value: str) -> None:
        self.chars.append(value)
        self.started = True

    def _flush(self) -> None:
        if not self.started:
            return
        word = "".join(self.chars)
        self.chars = []
        self.started = False
        kind, self.redirect = self.redirect, None
        if kind is None:
            self.segment.words.append(word)
        elif kind == "in":
            self.segment.inputs.append(word)
            self.segment.redirected.append(word)
        elif kind == "herestring":
            self.segment.stdin = word
        elif kind.startswith("heredoc"):
            self.heredocs.append((word, kind == "heredoc-", self.segment))

    def _close(self, piped: bool) -> None:
        self._flush()
        self.redirect = None
        segment = self.segment
        if segment.words or segment.inputs or segment.stdin is not None:
            self.segments.append(segment)
            self.segment = _Segment(piped)
        else:
            # Nothing was read since the pipe: "a |\n  grep x" and "a | (grep x)"
            # still feed the next command from that pipe.
            self.segment = _Segment(piped or segment.piped)

    def _ampersand(self, after: str) -> None:
        at = self.at
        if after == "&":
            self._close(False)
            self.at = at + 2
        elif after == ">" and self.dialect == "bash":
            self._flush()
            self.redirect = "out"
            self.at = at + (3 if self.text.startswith(">", at + 2) else 2)
        elif self.dialect != "bash" and not self.started and not self.segment.words:
            self.at = at + 1  # the call operator: "& python ..."
        else:
            self._close(False)
            self.at = at + 1

    def _heredoc_bodies(self) -> None:
        text, at = self.text, self.at
        for delimiter, strip_tabs, segment in self.heredocs:
            lines: list[str] = []
            while at < self.end:
                newline = text.find("\n", at)
                stop = self.end if newline < 0 else newline
                line = text[at:stop].rstrip("\r")
                at = stop + 1
                if strip_tabs:
                    line = line.lstrip("\t")
                if line == delimiter:
                    break
                lines.append(line)
            segment.stdin = "\n".join(lines)
        self.heredocs = []
        self.at = min(at, self.end)

    def _single_quoted(self) -> None:
        text, at = self.text, self.at + 1
        while at < self.end:
            char = text[at]
            if char == "'":
                if self.dialect == "pwsh" and text.startswith("'", at + 1):
                    self.chars.append("'")
                    at += 2
                    continue
                at += 1
                break
            self.chars.append(char)
            at += 1
        self.started = True
        self.at = at

    def _double_quoted(self) -> None:
        text, at, dialect = self.text, self.at + 1, self.dialect
        while at < self.end:
            char = text[at]
            after = text[at + 1] if at + 1 < self.end else ""
            if char == '"':
                if dialect == "pwsh" and after == '"':
                    self.chars.append('"')
                    at += 2
                    continue
                at += 1
                break
            if dialect == "bash" and char == "\\" and after and after in '$`"\\\n':
                if after != "\n":
                    self.chars.append(after)
                at += 2
            elif dialect == "pwsh" and char == "`" and after:
                self.chars.append(after)
                at += 2
            elif dialect != "cmd" and char == "$" and after == "(":
                close = _closing_paren(text, at + 1)
                self.nested.append(text[at + 2:close])
                self.chars.append(text[at:close + 1])
                at = close + 1
            elif dialect == "bash" and char == "`":
                close = text.find("`", at + 1)
                close = self.end if close < 0 else close
                self.nested.append(text[at + 1:close])
                at = close + 1
            else:
                self.chars.append(char)
                at += 1
        self.started = True
        self.at = at

    def _here_string(self) -> bool:
        text, at = self.text, self.at
        quote = text[at + 1]
        newline = text.find("\n", at + 2)
        if newline < 0 or text[at + 2:newline].strip():
            return False
        closing = text.find("\n" + quote + "@", newline)
        stop = self.end if closing < 0 else closing
        self._take(text[newline + 1:stop].rstrip("\r"))
        self.at = self.end if closing < 0 else closing + 3
        return True

    def _ansi_c_quoted(self) -> None:
        text, at = self.text, self.at + 2
        while at < self.end:
            char = text[at]
            if char == "\\" and at + 1 < self.end:
                self.chars.append(text[at + 1])
                at += 2
                continue
            at += 1
            if char == "'":
                break
            self.chars.append(char)
        self.started = True
        self.at = at

    def _redirection(self) -> None:
        text, at = self.text, self.at
        word = "".join(self.chars)
        if self.started and (word.isdigit() or (self.dialect == "pwsh" and word == "*")):
            self.chars = []  # a file-descriptor prefix such as 2> or *>
            self.started = False
        else:
            self._flush()
        if self.dialect == "pwsh" and text.startswith("<#", at):
            close = text.find("#>", at + 2)
            self.at = self.end if close < 0 else close + 2
        elif text.startswith("<<<", at):
            self.redirect, self.at = "herestring", at + 3
        elif self.dialect == "bash" and text.startswith("<<-", at):
            self.redirect, self.at = "heredoc-", at + 3
        elif self.dialect == "bash" and text.startswith("<<", at):
            self.redirect, self.at = "heredoc", at + 2
        elif text.startswith(("<(", ">("), at):
            if text[at] == "<":
                # "rg foo <(git show X:p)" reads a pipe, not the tree: "-" is that stream.
                self.segment.inputs.append("-")
            self.at = at + 1  # process substitution: "(" opens its own segment
        elif text[at] == "<":
            self.redirect = "out" if text.startswith("<&", at) else "in"
            self.at = at + (2 if text.startswith(("<&", "<>"), at) else 1)
        else:
            stop = at + 1
            if stop < self.end and text[stop] in ">|":
                stop += 1
            if stop < self.end and text[stop] == "&":
                stop += 1
            self.redirect, self.at = "out", stop


def _expand(text: str) -> str:
    return _VARIABLE.sub(lambda match: os.environ.get(match.group(1), match.group(0)), text)


def _git_ignored(path: Path, root: Path) -> bool:
    try:
        # Only the guarded root's own repo answers; without the ceiling git walks above a non-repo root.
        result = subprocess.run(
            ["git", "-C", str(path.parent), "check-ignore", "-q", "--", path.name],
            capture_output=True,
            timeout=2,
            env={**os.environ, "GIT_CEILING_DIRECTORIES": str(root.parent)},
        )
        return result.returncode == 0
    except Exception:
        return False


class IndexPolicy:
    """The Source Graph policy fields that decide whether a path is indexed."""

    __slots__ = ("exclude_dirs", "exclude_globs", "include_roots", "indexed_extensions")

    def __init__(
        self,
        exclude_dirs: frozenset[str],
        exclude_globs: tuple[str, ...],
        include_roots: tuple[str, ...],
        indexed_extensions: frozenset[str],
    ) -> None:
        self.exclude_dirs = exclude_dirs
        self.exclude_globs = exclude_globs
        self.include_roots = include_roots
        self.indexed_extensions = indexed_extensions


def glob_ignored(relative_path: str, patterns: tuple[str, ...], *, is_dir: bool = False) -> bool:
    relative_path = relative_path.strip("/")
    for pattern in patterns:
        normalized = pattern.strip().strip("/")
        if not normalized:
            continue
        if fnmatch.fnmatchcase(relative_path, normalized):
            return True
        # ``foo/**`` must prune ``foo`` before os.walk descends into it.
        if is_dir and normalized.endswith("/**"):
            base = normalized[:-3].rstrip("/")
            if relative_path == base or relative_path.startswith(f"{base}/"):
                return True
    return False


def under_include_root(rel: str, roots: tuple[str, ...]) -> bool:
    """Component-wise prefix test: ``vendor/lib`` does not match ``vendor/library``."""
    return any(rel == root or rel.startswith(root + "/") for root in roots)


def index_admits(policy: IndexPolicy, rel: str, *, is_dir: bool, ignored: bool) -> bool:
    """The one indexed-path predicate Source Graph and this guard share.

    ``rel`` is repository-relative POSIX. Only its last component and the whole
    path are judged, so the caller admits every ancestor first: ``os.walk``
    pruning does that for ``source_graph.iter_source_files``. ``ignored`` says
    git ignores ``rel`` or an ancestor; an include root re-admits it, and a
    directory above an include root stays open so the walk can reach it.
    """

    name = rel.rpartition("/")[2]
    if is_dir:
        if name in policy.exclude_dirs or name.endswith(".egg-info"):
            return False
        if glob_ignored(rel, policy.exclude_globs, is_dir=True):
            return False
    else:
        # ``PurePath.suffix``, without building a path per walked file.
        dot = name.rfind(".")
        suffix = name[dot:] if 0 < dot < len(name) - 1 else ""
        if suffix.lower() not in policy.indexed_extensions:
            return False
        if glob_ignored(rel, policy.exclude_globs):
            return False
    if not ignored or under_include_root(rel, policy.include_roots):
        return True
    return is_dir and any(root.startswith(rel + "/") for root in policy.include_roots)


def load_index_policy(root: Path) -> IndexPolicy:
    """Read Source Graph's repository policy file for the guard.

    ``source_graph.load_ignore_policy`` validates the same file and refuses a
    malformed one, indexing nothing. The guard never refuses, so an absent or
    malformed file leaves the default policy guarded.
    """

    default = IndexPolicy(EXCLUDED_DIRS, DEFAULT_EXCLUDE_GLOBS, (), INDEXED_SUFFIXES)
    try:
        path = root.joinpath(*_POLICY_PARTS)
        if not path.is_file() or path.stat().st_size > _POLICY_MAX_BYTES:
            return default
        payload = json.loads(path.read_text(encoding="utf-8"))
        fields = [
            payload.get(key, [])
            for key in ("exclude_dirs", "exclude_globs", "include_roots", "disabled_languages")
        ]
    except (OSError, ValueError, AttributeError):
        return default
    if not all(
        isinstance(field, list) and all(isinstance(item, str) and item.strip() for item in field)
        for field in fields
    ):
        return default
    dirs, globs, roots, disabled = ([item.strip().replace("\\", "/") for item in field] for field in fields)
    if not set(disabled) <= LANGUAGE_EXTENSIONS.keys():
        return default
    return IndexPolicy(
        EXCLUDED_DIRS | frozenset(dirs),
        tuple(dict.fromkeys(globs)),
        tuple(dict.fromkeys(item.rstrip("/") for item in roots)),
        frozenset(
            extension
            for language, extensions in LANGUAGE_EXTENSIONS.items()
            if language not in disabled
            for extension in extensions
        ),
    )


def _like_prefix(rel: str) -> str:
    """A LIKE pattern matching every path below directory ``rel``."""
    escaped = rel.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "/%"


def _connect_readonly_db(path: Path, timeout: float) -> sqlite3.Connection:
    """Open ``path`` read-only through an SQLite ``mode=ro`` URI.

    The one SQLite open in this hook: Source Graph membership and the Stop
    guard's task-queue read share it, so neither can write or create a file.
    """
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=timeout)
    try:
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        conn.close()
        raise
    return conn


class SourceGraphMembership:
    """Source Graph's own answer to "is this path indexed?", read-only.

    ``member`` returns True when the current generation indexes the path (a
    directory: anything below it), False when that generation lists it as not
    indexed or omits it, and None when no generation is readable: a missing,
    locked or malformed database, or one whose build never finished. None
    leaves the verdict to ``IndexPolicy``. The connection is opened lazily,
    once per evaluation, and ``close`` releases it.
    """

    __slots__ = ("path", "_conn", "_opened")

    def __init__(self, root: Path) -> None:
        self.path = root.joinpath(*SOURCE_GRAPH_DB_PARTS)
        self._conn: sqlite3.Connection | None = None
        self._opened = False

    def _connection(self) -> sqlite3.Connection | None:
        if self._opened:
            return self._conn
        self._opened = True
        try:
            if not self.path.is_file():
                return None
            conn = _connect_readonly_db(self.path, _SOURCE_GRAPH_BUSY_SECONDS)
        except (sqlite3.Error, OSError, ValueError):
            return None
        try:
            # A generation exists once a build has published ``meta.last_build``.
            if conn.execute("SELECT 1 FROM meta WHERE key = 'last_build'").fetchone() is None:
                conn.close()
                return None
        except sqlite3.Error:
            conn.close()
            return None
        self._conn = conn
        return conn

    def member(self, rel: str, is_dir: bool) -> bool | None:
        conn = self._connection()
        if conn is None:
            return None
        statuses = ",".join("?" * len(UNINDEXED_STATUSES))
        indexed = f" AND status NOT IN ({statuses}) LIMIT 1"
        if not is_dir:
            exact = ("SELECT 1 FROM files WHERE file_path = ?" + indexed, (rel,))
            # A case-insensitive file system hands over the caller's spelling.
            folded = ("SELECT 1 FROM files WHERE file_path = ? COLLATE NOCASE" + indexed, (rel,))
        elif rel == ".":
            exact = folded = ("SELECT 1 FROM files WHERE 1" + indexed, ())
        else:
            exact = (
                "SELECT 1 FROM files WHERE file_path >= ? AND file_path < ?" + indexed,
                (rel + "/", rel + "0"),  # "0" is the code point after "/"
            )
            folded = (
                "SELECT 1 FROM files WHERE file_path LIKE ? ESCAPE '\\'" + indexed,
                (_like_prefix(rel),),
            )
        try:
            for sql, params in (exact, folded):
                if conn.execute(sql, (*params, *UNINDEXED_STATUSES)).fetchone() is not None:
                    return True
        except sqlite3.Error:
            return None
        return False

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()


def _worktree_mirror(parts: tuple[str, ...]) -> tuple[tuple[str, ...], str] | None:
    """HEAD + 32 lowercase hex + ``worktree`` + rest, else None.

    ``rest`` is classified like the canonical path. The worktree path is taken
    from these parts so a deny reason never reads a shared cell.
    """
    head = _WORKTREE_MIRROR_HEAD
    if (
        len(parts) < len(head) + 2
        or parts[: len(head)] != head
        or parts[len(head) + 1] != "worktree"
        or _WORKTREE_ID.fullmatch(parts[len(head)]) is None
    ):
        return None
    return parts[len(head) + 2 :], "/".join(parts[: len(head) + 2])


class _Context:
    """Resolve operands against the repository root and the current directory."""

    def __init__(
        self,
        root: Path,
        cwd: Path | None,
        dialect: str,
        policy: IndexPolicy,
        membership: SourceGraphMembership | None = None,
    ) -> None:
        self.root = root
        self.cwd = cwd
        self.dialect = dialect
        self.policy = policy
        # Callers that omit it still consult Source Graph's own membership for
        # ``root``; its connection opens lazily, only when a verdict needs it.
        self.membership = SourceGraphMembership(root) if membership is None else membership

    def fork(self, dialect: str | None = None) -> _Context:
        return _Context(self.root, self.cwd, dialect or self.dialect, self.policy, self.membership)

    def moved(self, operand: str) -> _Context:
        scope = self.fork()
        # An unresolvable base keeps the retained cwd (fail closed).
        scope.cwd = self.path(operand) or self.cwd
        return scope

    def _text(self, operand: str) -> str:
        text = _expand(operand.strip())
        if os.sep == "\\" or self.dialect != "bash":
            text = text.replace("\\", "/")
        if os.sep == "\\" and self.dialect == "bash":
            match = _MSYS_DRIVE.fullmatch(text)
            if match is not None:
                text = match.group(1).upper() + ":" + (match.group(2) or "/")
        return text

    def path(self, operand: str, base: Path | None = None) -> Path | None:
        text = self._text(operand)
        # An unresolved leading variable or substitution names an unknown place.
        if not text or "\x00" in text or text.startswith("$"):
            return None
        if text == "~" or text.startswith("~/"):
            text = os.path.expanduser(text)
        candidate = Path(text)
        if not candidate.is_absolute():
            anchor = base if base is not None else self.cwd
            if anchor is None:
                return None
            candidate = anchor / candidate
        try:
            return Path(os.path.realpath(candidate))
        except (OSError, ValueError):
            return None

    def inside(self, path: Path | None) -> tuple[str, ...] | None:
        if path is None:
            return None
        try:
            return path.relative_to(self.root).parts
        except ValueError:
            return None

    def indexed_rel(self, path: Path | None, is_dir: bool = True) -> str | None:
        parts = self.inside(path)
        if path is None or parts is None:
            return None
        mirror = _worktree_mirror(parts)
        classified = mirror[0] if mirror is not None else parts
        if classified:
            policy = self.policy
            for depth in range(1, len(classified)):
                if not index_admits(policy, "/".join(classified[:depth]), is_dir=True, ignored=False):
                    return None
            rel = "/".join(classified)
            if not index_admits(policy, rel, is_dir=is_dir, ignored=False):
                return None
            # Source Graph's own generation outranks the policy: a declared but
            # not (yet) indexed path is unindexed, and a member needs no git.
            member = self.membership.member(rel, is_dir)
            if member is False:
                return None
            # git is asked only when its answer can change the verdict; it
            # reports a path below an ignored directory as ignored too.
            if (
                member is None
                and not index_admits(policy, rel, is_dir=is_dir, ignored=True)
                and _git_ignored(path, self.root)
            ):
                return None
        elif not is_dir or self.membership.member(".", True) is False:
            return None
        # Keep the denied path so the reason derives this worktree, not a cell.
        return "/".join(parts) or "."

    def indexed_file(self, operand: str, glob: bool = True) -> str | None:
        if glob and _GLOB.search(operand):
            return self.indexed_glob(operand)
        return self.indexed_rel(self.path(operand), is_dir=False)

    def indexed_dir(self, operand: str) -> str | None:
        if _GLOB.search(operand):
            return self.indexed_glob(operand)
        path = self.path(operand)
        if path is None or not path.is_dir():
            return None
        return self.indexed_rel(path)

    def indexed(self, operand: str) -> str | None:
        return self.indexed_file(operand) or self.indexed_dir(operand)

    def indexed_glob(self, operand: str) -> str | None:
        text = self._text(operand)
        parts = text.split("/")
        first = next((at for at, part in enumerate(parts) if _GLOB.search(part)), len(parts) - 1)
        base = self.path("/".join(parts[:first]) or ("/" if text.startswith("/") else "."))
        rel = self.indexed_rel(base)
        tail = parts[first:]
        excluded = self.policy.exclude_dirs
        if base is None or rel is None or any(part in excluded for part in tail):
            return None
        suffix = _SUFFIX.search(tail[-1])
        if suffix is not None:
            if "." + suffix.group(1).lower() not in self.policy.indexed_extensions:
                return None
        elif not base.is_dir():
            return None
        shown = "/".join(tail)
        return shown if rel == "." else rel + "/" + shown

    def editable(self, path: Path | None) -> str | None:
        parts = self.inside(path)
        if not parts or any(part.lower() in _EDIT_EXEMPT_DIRS for part in parts):
            return None
        return "/".join(parts) if path is not None and path.is_file() else None

    def scratch(self, path: Path | None) -> str | None:
        """The repository-relative path when ``path`` lies in ``SCRATCH_DIR``, existing or not."""
        parts = self.inside(path)
        if not parts or parts[0].lower() != SCRATCH_DIR:
            return None
        return "/".join(parts)

    def change_directory(self, args: list[str]) -> None:
        target = None
        at = 0
        while at < len(args):
            arg = args[at]
            at += 1
            if arg.lower() in ("-path", "-literalpath", "-lp") and at < len(args):
                target = args[at]
                at += 1
            elif arg.lower() != "/d" and not (arg.startswith("-") and len(arg) > 1):
                target = arg
        if target is None and self.dialect == "bash":
            target = "~"
        if target is None or target == "-":
            return
        # An unresolvable target ("$TMPDIR/x" with TMPDIR unset) is an unknown
        # place, not an outside one: every rule keeps the retained cwd.
        self.cwd = self.path(target) or self.cwd


def _gnu(
    args: list[str], short_values: str = "", long_values: frozenset[str] = frozenset()
) -> tuple[set[str], list[tuple[str, str]], list[str]]:
    """Split GNU-style argv into (flags seen, (option, value) pairs, operands)."""

    flags: set[str] = set()
    values: list[tuple[str, str]] = []
    operands: list[str] = []
    at = 0
    while at < len(args):
        arg = args[at]
        at += 1
        if arg == "--":
            operands.extend(args[at:])
            break
        if arg.startswith("--"):
            name, equals, value = arg[2:].partition("=")
            flags.add("--" + name)
            if equals:
                values.append(("--" + name, value))
            elif name in long_values and at < len(args):
                values.append(("--" + name, args[at]))
                at += 1
            continue
        if len(arg) < 2 or arg[0] != "-":
            operands.append(arg)
            continue
        for position in range(1, len(arg)):
            flags.add("-" + arg[position])
            if arg[position] in short_values:
                if position + 1 < len(arg):
                    values.append(("-" + arg[position], arg[position + 1:]))
                elif at < len(args):
                    values.append(("-" + arg[position], args[at]))
                    at += 1
                break
    return flags, values, operands


def _ps_bind(
    args: list[str], params: tuple[str, ...], switches: tuple[str, ...], aliases: dict[str, str]
) -> tuple[dict[str, list[str]], list[str]]:
    """Bind PowerShell ``-Name value`` arguments; a name matches by prefix."""

    named: dict[str, list[str]] = {}
    positionals: list[str] = []
    names = params + switches
    at = 0
    while at < len(args):
        arg = args[at]
        at += 1
        if len(arg) < 2 or arg[0] != "-" or not (arg[1].isalpha() or arg[1] == "_"):
            positionals.append(arg)
            continue
        raw, colon, value = arg[1:].partition(":")
        key = raw.lower()
        if key in aliases:
            key = aliases[key]
        elif key not in names:
            key = next((name for name in names if name.startswith(key)), key)
        if key in params and not colon:
            value = args[at] if at < len(args) else ""
            at += 1
        named.setdefault(key, []).append(value)
    return named, positionals


def _split_list(values: list[str]) -> list[str]:
    return [part for value in values for part in value.split(",") if part]


def _command_name(word: str) -> str:
    match = _VARIABLE.fullmatch(word)
    if match is not None:
        variable = match.group(1)
        if os.environ.get(variable):
            word = os.environ[variable]
        elif "PYTHON" in variable.upper():
            word = "python"
        elif "NODE" in variable.upper():
            word = "node"
    name = re.split(r"[\\/]", word.strip())[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def _command_words(words: list[str], dialect: str) -> list[str]:
    """Drop keywords, assignments and wrapper commands (sudo, env, xargs, ...)."""

    at = 0
    while at < len(words):
        word = words[at]
        if word.lower() in _KEYWORDS or (dialect == "bash" and _ASSIGNMENT.match(word)):
            at += 1
            continue
        if dialect == "pwsh" and word.startswith("$"):
            if at + 1 < len(words) and _PS_ASSIGNMENT.fullmatch(words[at + 1]):
                at += 2
                continue
            _, equals, rest = word.partition("=")
            if equals and rest:
                return [rest] + words[at + 1:]
            return words[at:]
        name = _command_name(word)
        if name not in _WRAPPERS:
            return words[at:]
        at += 1
        while at < len(words) and words[at].startswith("-") and words[at] != "-":
            option = words[at]
            at += 1
            if option == "--":
                break
            if option in _WRAPPERS[name]:
                at += 1
        if name == "timeout":
            at += 1
    return []


# GNU grep searches recursively only on request, and then never reads stdin.
_GREPS = frozenset({"grep", "egrep", "fgrep"})


def _search_terms(name: str, args: list[str]) -> tuple[str, list[str], bool] | None:
    """``(pattern, operands, recursive)`` of a search command, or None if it searches nothing."""

    if name == "findstr":
        return _findstr_terms(args)
    if name in ("select-string", "sls"):
        named, positionals = _ps_bind(args, _SLS_PARAMS, _SLS_SWITCHES, _PS_ALIASES)
        patterns = named.get("pattern", [])
        if not patterns:
            if not positionals:
                return None
            patterns = [positionals.pop(0)]
        paths = named.get("path", []) + named.get("literalpath", []) + positionals
        return patterns[0], _split_list(paths), False
    if name == "rg":
        flags, values, operands = _gnu(args, _RG_SHORT, _RG_LONG)
        named_patterns: tuple[str, ...] = ("-e", "--regexp")
    elif name in ("ag", "ack", "ack-grep"):
        flags, values, operands = _gnu(args, "ABCGgmpW", _AG_LONG)
        named_patterns = ("--match",)
    else:
        flags, values, operands = _gnu(args, "ABCDdefm", _GREP_LONG)
        named_patterns = ("-e", "--regexp")
    patterns = [value for option, value in values if option in named_patterns]
    if not patterns and not any(option in ("-f", "--file") for option, _ in values):
        if not operands:
            return None
        patterns.append(operands.pop(0))
    recursive = True
    if name in _GREPS:
        recursive = bool(flags & {"-r", "-R", "--recursive", "--dereference-recursive"}) or any(
            option in ("-d", "--directories") and value == "recurse" for option, value in values
        )
    return (patterns[0] if patterns else ""), operands, recursive


def _findstr_terms(args: list[str]) -> tuple[str, list[str], bool] | None:
    patterns: list[str] = []
    operands: list[str] = []
    directories: list[str] = []
    recursive = from_option = False
    for arg in args:
        match = _FINDSTR_OPTION.fullmatch(arg)
        if match is None:
            operands.append(arg)
            continue
        letters, value = match.group(1).lower(), match.group(2)
        head = letters if value is None else letters[:-1]
        if letters != "offline" and "s" in head:
            recursive = True
        if value is None:
            continue
        if letters.endswith("c"):
            patterns.append(value)
            from_option = True
        elif letters.endswith("g"):
            from_option = True
        elif letters.endswith("d"):
            directories.extend(part for part in value.split(";") if part)
    if not from_option:
        if not operands:
            return None
        patterns.append(operands.pop(0))
    return (patterns[0] if patterns else ""), operands + directories, recursive


def _search_verdict(
    ctx: _Context, segment: _Segment, name: str, terms: tuple[str, list[str], bool]
) -> tuple[str, str, str] | None:
    pattern, operands, recursive = terms
    explicit = [operand for operand in operands + segment.inputs if operand]
    for operand in explicit:
        target = ctx.indexed(operand) if operand != "-" else None
        if target:
            return ("R1", target, pattern)
    files = list(explicit)
    for redirected in segment.redirected:
        if redirected in files:
            files.remove(redirected)  # "< file" feeds stdin; it is no FILE operand
    if files or not recursive:
        return None
    # A pipe consumer with no file operand searches its stdin, not the tree;
    # a recursive grep with no FILE ignores stdin and searches the directory.
    stdin = segment.piped or segment.stdin is not None or bool(segment.redirected)
    if stdin and name not in _GREPS:
        return None
    target = ctx.indexed_rel(ctx.cwd)
    return ("R1", target, pattern) if target else None


def _git_verdict(ctx: _Context, args: list[str]) -> tuple[str, str, str] | None:
    where = ctx.cwd
    at = 0
    while at < len(args) and args[at].startswith("-"):
        option = args[at]
        at += 1
        if option in _GIT_GLOBAL_VALUED and at < len(args):
            if option == "-C":
                where = ctx.path(args[at], where) or where
            at += 1
    if at >= len(args) or args[at] != "grep":
        return None
    inside = ctx.inside(where)
    if inside is None:
        return None
    _, values, operands = _gnu(args[at + 1:], "ABCefm", _GIT_GREP_LONG)
    patterns = [value for option, value in values if option in ("-e", "--regexp")]
    pattern = patterns[0] if patterns else (operands[0] if operands else "")
    return ("R1", "/".join(inside) or ".", pattern)


def _listing_verdict(ctx: _Context, targets: list[str]) -> tuple[str, str, str] | None:
    for target in targets or ["."]:
        rel = ctx.indexed_dir(target)
        if rel:
            return ("R2", rel, "")
    return None


def _listing_command(ctx: _Context, name: str, args: list[str]) -> tuple[str, str, str] | None:
    if name in ("get-childitem", "gci") or (ctx.dialect == "pwsh" and name in ("ls", "dir")):
        named, positionals = _ps_bind(args, _GCI_PARAMS, _GCI_SWITCHES, _GCI_ALIASES)
        if "recurse" not in named and "depth" not in named:
            return None
        paths = named.get("path", []) + named.get("literalpath", []) + positionals[:1]
        return _listing_verdict(ctx, _split_list(paths))
    if name == "find":
        at = 0
        while at < len(args) and (args[at] in ("-H", "-L", "-P") or args[at].startswith("-O")):
            at += 1
        if at < len(args) and args[at] == "-D":
            at += 2
        starts: list[str] = []
        for arg in args[at:]:
            if arg.startswith("-") or arg in ("!", "(", ")", ","):
                break
            starts.append(arg)
        return _listing_verdict(ctx, starts)
    if name in ("fd", "fdfind"):
        stop = next(
            (at for at, arg in enumerate(args) if arg in ("-x", "-X", "--exec", "--exec-batch")),
            len(args),
        )
        _, values, operands = _gnu(args[:stop], "dEteSjco", _FD_LONG)
        base = next((value for option, value in values if option == "--base-directory"), None)
        scope = ctx if base is None else ctx.moved(base)
        searched = [value for option, value in values if option == "--search-path"]
        return _listing_verdict(scope, operands[1:] + searched)
    if name == "tree":
        kept = [arg for arg in args if not _CMD_SWITCH.fullmatch(arg)]
        return _listing_verdict(ctx, _gnu(kept, "LPIoHT", _TREE_LONG)[2])
    switches = [arg for arg in args if _CMD_SWITCH.fullmatch(arg)] if name == "dir" else []
    flags, _, operands = _gnu([arg for arg in args if arg not in switches], "ITw", _LS_LONG)
    recursive = "-R" in flags or "--recursive" in flags or any(
        letter in "sS" for arg in switches for letter in re.findall(r"/([A-Za-z])", arg)
    )
    return _listing_verdict(ctx, operands) if recursive else None


def _read_verdict(
    ctx: _Context, segment: _Segment, files: list[str], rule: str
) -> tuple[str, str, str] | None:
    for operand in files:
        target = ctx.indexed_file(operand) if operand and operand != "-" else None
        if target:
            return (rule, target, "")
    for operand in segment.inputs:
        target = ctx.indexed(operand) if operand and operand != "-" else None
        if target:
            return (rule, target, "")
    return None


def _reader_command(
    ctx: _Context, segment: _Segment, name: str, args: list[str]
) -> tuple[str, str, str] | None:
    rule = "R3"
    if name in ("get-content", "gc") or (ctx.dialect == "pwsh" and name in ("cat", "type")):
        named, positionals = _ps_bind(args, _GC_PARAMS, _GC_SWITCHES, _PS_ALIASES)
        files = _split_list(named.get("path", []) + named.get("literalpath", []) + positionals)
    elif name == "type":
        files = [arg for arg in args if not arg.startswith(("-", "/"))]
    elif name == "cat":
        files = _gnu(args)[2]
    elif name in ("head", "tail"):
        files = [arg for arg in _gnu(args, "ncs", _HEAD_LONG)[2] if not re.fullmatch(r"\+\d+", arg)]
    elif name == "sed":
        flags, values, files = _gnu(args, "efl", _SED_LONG)
        if not any(option in ("-e", "--expression", "-f", "--file") for option, _ in values):
            files = files[1:]
        if "-i" in flags or "--in-place" in flags:
            rule = "R6"  # an in-place sed is an edit: point at the semantic editor
    elif name in ("awk", "gawk", "mawk", "nawk"):
        _, values, files = _gnu(args, "EFfilv", _AWK_LONG)
        if not any(option in ("-f", "--file", "-E", "--exec") for option, _ in values):
            files = files[1:]
        files = [arg for arg in files if not _ASSIGNMENT.match(arg)]
    elif name in ("less", "more"):
        files = _gnu([arg for arg in args if not arg.startswith("+")], "bhjkoOpPtTxyz#")[2]
    elif name == "nl":
        files = _gnu(args, "bdfhilnsvw", _NL_LONG)[2]
    else:
        files = _gnu(args, "lHmr", _BAT_LONG)[2]
    return _read_verdict(ctx, segment, files, rule)


def _python_program(args: list[str]) -> str | None:
    """The inline program of a python argv: "" means stdin, None means a file or module."""

    at = 0
    while at < len(args):
        arg = args[at]
        at += 1
        if arg == "-":
            return ""
        if arg.startswith("--"):
            if arg == "--check-hash-based-pycs":
                at += 1
            continue
        if not arg.startswith("-") or len(arg) < 2:
            return None
        for position in range(1, len(arg)):
            letter = arg[position]
            if letter == "c":
                if position + 1 < len(arg):
                    return arg[position + 1:]
                return args[at] if at < len(args) else None
            if letter == "m":
                return None
            if letter in "WX":
                if position == len(arg) - 1:
                    at += 1
                break
    return ""


def _node_program(args: list[str]) -> str | None:
    at = 0
    while at < len(args):
        arg = args[at]
        at += 1
        if arg in ("-e", "--eval", "-p", "--print", "-pe"):
            return args[at] if at < len(args) else None
        if arg.startswith(("--eval=", "--print=")):
            return arg.partition("=")[2]
        if arg == "-":
            return ""
        if arg in _NODE_VALUED:
            at += 1
        elif not arg.startswith("-"):
            return None
    return ""


def _program_verdict(ctx: _Context, program: str) -> tuple[str, str, str] | None:
    reads = _READ_CALL.search(program) is not None
    lists = _LIST_CALL.search(program) is not None
    globs = "glob(" in program
    if not (reads or lists):
        return None
    seen: set[str] = set()
    for match in _LITERAL.finditer(program):
        literal = match.group(1) or match.group(2) or match.group(3) or ""
        if (
            not literal
            or literal in seen
            or len(literal) > 512
            or "://" in literal
            or any(char.isspace() for char in literal)
        ):
            continue
        seen.add(literal)
        if len(seen) > _LITERALS_MAX:
            break
        text = literal.replace("\\\\", "/").replace("\\", "/")
        if reads:
            path = ctx.path(text)
            if path is not None and path.is_file():
                target = ctx.indexed_rel(path, is_dir=False)
                if target:
                    return ("R4", target, "")
        # Only glob() expands a wildcard: listdir, scandir, walk, iterdir and readdir
        # take a literal directory, so without glob() a wildcard literal lists nothing.
        if lists and (globs or not _GLOB.search(text)):
            target = ctx.indexed_dir(text)
            if target:
                return ("R4", target, "")
    return None


def _interpreter_verdict(
    ctx: _Context, segment: _Segment, name: str, args: list[str], previous: _Segment | None
) -> tuple[str, str, str] | None:
    program = _node_program(args) if name.startswith("node") else _python_program(args)
    if program == "":
        if segment.stdin is not None:
            program = segment.stdin
        elif segment.piped and previous is not None:
            program = previous.text()
    return _program_verdict(ctx, program) if program else None


def _decode_command(value: str) -> str:
    try:
        return base64.b64decode(value).decode("utf-16-le")
    except ValueError:
        return ""


def _nested_program(name: str, args: list[str]) -> tuple[str, str] | None:
    """``(dialect, program)`` of a shell launched with an inline command."""

    if name in _POSIX_SHELLS:
        at = 0
        while at < len(args):
            arg = args[at]
            at += 1
            if arg in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
                at += 1
            elif arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
                return ("bash", args[at]) if at < len(args) else None
            elif not arg.startswith(("-", "+")):
                return None
        return None
    if name in ("powershell", "pwsh"):
        at = 0
        while at < len(args):
            arg = args[at]
            at += 1
            if arg[:1] not in ("-", "/"):
                return ("pwsh", " ".join(args[at - 1:])) if name == "powershell" else None
            key = arg.lstrip("-/").lower()
            if key and "command".startswith(key):
                return ("pwsh", " ".join(args[at:])) if at < len(args) else None
            if key in ("e", "ec") or (len(key) > 1 and "encodedcommand".startswith(key)):
                return ("pwsh", _decode_command(args[at])) if at < len(args) else None
            if key and "file".startswith(key):
                return None
            if key in _PWSH_VALUED:
                at += 1
        return None
    if name == "cmd":
        for at, arg in enumerate(args):
            if arg.lower() in ("/c", "/k", "/r"):
                return ("cmd", " ".join(args[at + 1:])) if at + 1 < len(args) else None
            if not arg.startswith("/"):
                return None
        return None
    if name in ("invoke-expression", "iex"):
        named, positionals = _ps_bind(args, ("command",), (), {})
        program = named.get("command", []) + positionals
        return ("pwsh", " ".join(program)) if program else None
    return None


def _command_verdict(
    ctx: _Context,
    name: str,
    args: list[str],
    segment: _Segment,
    previous: _Segment | None,
    depth: int,
) -> tuple[str, str, str] | None:
    if name in _SEARCHERS:
        if name == "rg" and "--files" in args:
            return _listing_verdict(ctx, _gnu(args, _RG_SHORT, _RG_LONG)[2])
        terms = _search_terms(name, args)
        return None if terms is None else _search_verdict(ctx, segment, name, terms)
    if name == "git":
        return _git_verdict(ctx, args)
    if name in _LISTERS:
        return _listing_command(ctx, name, args)
    if name in _READERS:
        return _reader_command(ctx, segment, name, args)
    if _INTERPRETER.fullmatch(name):
        return _interpreter_verdict(ctx, segment, name, args, previous)
    nested = _nested_program(name, args)
    if nested is None:
        return None
    return _shell_verdict(ctx.fork(nested[0]), nested[1], depth + 1)


def _xargs_operands(ctx: _Context, producer: _Segment) -> list[str]:
    """The file operands ``producer | xargs consumer`` hands to the consumer."""

    words = _command_words(producer.words, ctx.dialect)
    if not words:
        return []
    if _command_name(words[0]) == "git" and "ls-files" in words:
        rest = words[words.index("ls-files") + 1:]
        return [word for word in rest if not word.startswith("-")] or ["."]
    return [word for word in words[1:] if not word.startswith("-")]


def _piped_search(
    ctx: _Context, consumer: _Segment, verdict: tuple[str, str, str]
) -> tuple[str, str, str]:
    """A recursive listing piped into a search is a search: name bodygrep instead."""

    words = _command_words(consumer.words, ctx.dialect) if consumer.piped else []
    name = _command_name(words[0]) if words else ""
    terms = _search_terms(name, words[1:]) if name in _SEARCHERS else None
    return verdict if terms is None else ("R1", verdict[1], terms[0])


def _shell_verdict(ctx: _Context, command: str, depth: int = 0) -> tuple[str, str, str] | None:
    if depth > _NESTING_MAX or len(command) > _COMMAND_MAX_CHARS:
        return None
    segments, nested = _Lexer(command, ctx.dialect).run()
    previous: _Segment | None = None
    for index, segment in enumerate(segments):
        words = _command_words(segment.words, ctx.dialect)
        wrappers = segment.words[: len(segment.words) - len(words)]
        if previous is not None and segment.piped and "xargs" in map(_command_name, wrappers):
            # xargs turns the producer's output into operands: they are files, not text.
            segment.inputs.extend(_xargs_operands(ctx, previous))
            segment.piped = False
        name = _command_name(words[0]) if words else ""
        if name in _DIRECTORY_CHANGERS:
            ctx.change_directory(words[1:])
        elif name:
            verdict = _command_verdict(ctx, name, words[1:], segment, previous, depth)
            if verdict is not None:
                if verdict[0] == "R2" and index + 1 < len(segments):
                    verdict = _piped_search(ctx, segments[index + 1], verdict)
                return verdict
        previous = segment
    for program in nested:
        verdict = _shell_verdict(ctx.fork(), program, depth + 1)
        if verdict is not None:
            return verdict
    return None


def _write_verdict(ctx: _Context, file_path: str) -> tuple[str, str, str] | None:
    target = ctx.editable(ctx.path(file_path))
    return None if target is None else ("R7", target, "")


_IMAGE_SIGNATURES: dict[str, tuple[tuple[int, bytes], ...]] = {
    ".png": ((0, b"\x89PNG\r\n\x1a\n"),),
    ".jpg": ((0, b"\xff\xd8\xff"),),
    ".jpeg": ((0, b"\xff\xd8\xff"),),
    ".webp": ((0, b"RIFF"), (8, b"WEBP")),
}


def _own_scratch_image(scratch: str, path: Path, session_id: object) -> bool:
    """True for a real PNG/JPEG/WEBP file under ``.scratch/<session_id>/`` (R8 exception)."""
    if not isinstance(session_id, str) or not session_id:
        return False
    parts = scratch.split("/")
    if len(parts) < 3 or parts[1] != session_id:
        return False
    signature = _IMAGE_SIGNATURES.get(os.path.splitext(parts[-1])[1].lower())
    if signature is None:
        return False
    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
    except OSError:
        return False
    return all(head[start : start + len(magic)] == magic for start, magic in signature)


def _tool_verdict(
    ctx: _Context, tool_name: str, tool_input: dict[str, object], session_id: object = None
) -> tuple[str, str, str] | None:
    if tool_name in ("Bash", "PowerShell"):
        command = tool_input.get("command")
        return _shell_verdict(ctx, command) if isinstance(command, str) else None
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        return None
    if tool_name in ("Read", "Edit", "MultiEdit", "Write"):
        path = ctx.path(file_path)
        scratch = ctx.scratch(path)
        # Write that creates a new scratch file is the create path R7 allows
        # everywhere; reading, editing and overwriting stay with the tools.
        creates = tool_name == "Write" and path is not None and not os.path.lexists(path)
        # A Read of the session's own rendered image is the one built-in Read
        # Source Graph cannot answer (it previews 8 bytes of an unindexed file).
        own_image = (
            tool_name == "Read"
            and scratch is not None
            and path is not None
            and _own_scratch_image(scratch, path, session_id)
        )
        # Source Graph answers excluded_by_policy, with no preview, for a scratch
        # file its policy excludes (a throwaway repo copy): a Read is the only way in.
        excluded_read = (
            tool_name == "Read"
            and scratch is not None
            and glob_ignored(scratch, ctx.policy.exclude_globs)
        )
        if scratch is not None and not creates and not own_image and not excluded_read:
            return "R8", scratch, ""
    if tool_name == "Read":
        target = ctx.indexed_file(file_path, glob=False)
        return None if target is None else ("R5", target, "")
    if tool_name in ("Edit", "MultiEdit"):
        target = ctx.editable(ctx.path(file_path))
        return None if target is None else ("R6", target, "")
    if tool_name == "Write":
        return _write_verdict(ctx, file_path)
    return None


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: max(0, limit - 3)] + "..."


def _quoted(value: str, limit: int) -> str:
    body = json.dumps(value, ensure_ascii=False)[1:-1]
    return '"' + _clip(body, max(3, limit - 2)) + '"'


def _mirror_reason(rule: str, rest: str, worktree: str) -> str:
    route = "git --no-optional-locks -C " + worktree + " diff"
    template = (
        "AIWorkHub tool guard "
        + rule
        + ": worktree mirror of indexed source {target} is denied. Use "
        + route
        + " and Source Graph mode=\"body\" or mode=\"file\" query=\"START-END\" on {target}."
        + _UNINDEXED
    )
    slots = template.count("{target}")
    share = max(16, (REASON_MAX_CHARS - len(template.format(target=""))) // max(1, slots))
    text = template.format(target=_clip(_CONTROL.sub(" ", rest).strip() or ".", share))
    return _clip(text, REASON_MAX_CHARS)


def _reason(rule: str, target: str, detail: str, manager_script: bool = False) -> str:
    mirror = _worktree_mirror(tuple(target.split("/")))
    if mirror is not None:
        rest, worktree = mirror
        return _mirror_reason(rule, "/".join(rest) or ".", worktree)
    # R8 offers no unindexed fallback: a scratch file Source Graph can answer is
    # read through it, and one it excludes never reaches R8 for a Read.
    template = _REASONS[rule] + ("" if rule == "R8" else _UNINDEXED)
    template = template.replace(
        "{script}", " or " + "/".join(_MANAGER_SCRIPT_PARTS) if manager_script else ""
    )
    pattern = _CONTROL.sub(" ", detail).strip() if rule == "R1" else ""
    fixed = len(template.format(target="", pattern='""'))
    slots = template.count("{target}") + template.count("{pattern}")
    share = max(16, (REASON_MAX_CHARS - fixed) // max(1, slots))
    text = template.format(
        target=_clip(_CONTROL.sub(" ", target).strip() or ".", share),
        pattern=_quoted(pattern, share),
    )
    return _clip(text, REASON_MAX_CHARS)


def _evaluate(
    tool_name: object,
    tool_input: object,
    root: str | os.PathLike[str],
    cwd: object = None,
    session_id: object = None,
) -> tuple[str, str, str] | None:
    """``(rule_id, reason, target)`` of the first rule the call breaks, else None."""

    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        return None
    base = Path(os.path.realpath(os.fspath(root)))
    if not (base / ".aiworkhub").is_dir():
        return None
    where = base
    if isinstance(cwd, (str, Path)) and str(cwd).strip():
        where = Path(os.path.realpath(base / cwd))
    dialect = "pwsh" if tool_name == "PowerShell" else "bash"
    membership = SourceGraphMembership(base)
    try:
        ctx = _Context(base, where, dialect, load_index_policy(base), membership)
        verdict = _tool_verdict(ctx, tool_name, tool_input, session_id)
    finally:
        membership.close()
    if verdict is None:
        return None
    rule, target, detail = verdict
    script = rule == "R8" and base.joinpath(*_MANAGER_SCRIPT_PARTS).is_file()
    return rule, _reason(rule, target, detail, script), target


def decide(
    tool_name: object,
    tool_input: object,
    root: str | os.PathLike[str],
    cwd: object = None,
    session_id: object = None,
) -> tuple[str, str] | None:
    """Return ``(rule_id, reason)`` when the tool call must be denied, else None.

    Pure: it reads the tree under ``root`` but writes nothing. ``root`` is the
    project directory, and a root without ``.aiworkhub/`` allows everything.
    ``cwd`` (default ``root``) anchors relative shell operands. ``session_id``
    only lets R8 pass a Read of the session's own scratch image; a Read of a
    scratch file the Source Graph policy excludes passes R8 without it.
    """

    verdict = _evaluate(tool_name, tool_input, root, cwd, session_id=session_id)
    return None if verdict is None else (verdict[0], verdict[1])


def _read_payload() -> object:
    stream = getattr(sys.stdin, "buffer", None)
    raw = stream.read(_STDIN_MAX_BYTES + 1) if stream is not None else sys.stdin.read().encode()
    if not raw or len(raw) > _STDIN_MAX_BYTES:
        return None
    return json.loads(raw.decode("utf-8-sig"))


def _record_denial(root: str, rule: str, tool: object, target: str, session_id: object) -> None:
    """Best-effort append of one denial row; the command text is never recorded."""

    try:
        path = Path(root).joinpath(*_DENIALS_PARTS)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > _DENIALS_ROTATE_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
        except OSError:
            pass
        row = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rule": rule,
            "tool": str(tool)[:64],
            "target": target[:512],
            "session_id": None if session_id is None else str(session_id)[:128],
        }
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:
        pass


# --- Stop guard: the direct-chat manager's mechanical wake (NF-2026-02110) ---

TASK_QUEUE_DB_PARTS = (".aiworkhub", "tasking", "task_queue.sqlite")
MANAGER_WATCH_PARTS = (".aiworkhub", "runtime", "manager_watch")
STOP_REVIEW_STATUSES = ("review", "review_ready")
STOP_PROCESSING_STATUSES = ("processing",)
STOP_REASON_MAX_CHARS = 1200
STOP_LISTED_TASKS_MAX = 10
HEARTBEAT_STALE_FACTOR = 3.0
_TASK_QUEUE_BUSY_SECONDS = 0.25
_HEARTBEAT_MAX_BYTES = 4096
_HEARTBEAT_MAX_INTERVAL = 86400.0
_CARD_JSON_MAX_CHARS = 1024 * 1024
_SESSION_ID_MAX_CHARS = 128
_SESSION_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_PLAIN_SHELL_ARG = re.compile(r"^[A-Za-z0-9._:/=-]+$")
_TASK_COLUMNS_REQUIRED = frozenset({"task_id", "status", "card_json"})
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87


def manager_watch_heartbeat_path(root: str | Path, session_id: object) -> Path | None:
    """The heartbeat file ``manager_callback_watch`` keeps for ``session_id``.

    The single owner of the path: the watcher writes it and the Stop guard
    reads it. ``session_id`` is reduced to ``[A-Za-z0-9._-]``; an empty or
    all-dot result has no heartbeat path.
    """

    if not isinstance(session_id, str):
        return None
    name = _SESSION_ID_UNSAFE.sub("_", session_id.strip())[:_SESSION_ID_MAX_CHARS]
    if not name.strip("."):
        return None
    return Path(root).joinpath(*MANAGER_WATCH_PARTS, name + ".json")


def _windows_pid_alive(ctypes_module: object, windll: object, pid: int) -> bool:
    kernel32 = windll("kernel32", use_last_error=True)  # type: ignore[operator]
    c_void_p = getattr(ctypes_module, "c_void_p")
    c_uint32 = getattr(ctypes_module, "c_uint32")
    kernel32.OpenProcess.restype = c_void_p
    kernel32.OpenProcess.argtypes = (c_uint32, getattr(ctypes_module, "c_int"), c_uint32)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        get_last_error = getattr(ctypes_module, "get_last_error", None)
        error = get_last_error() if callable(get_last_error) else 0
        # Access denied (5) means the process exists; only "no such process"
        # (87) is dead. Anything unknown is alive: the guard fails open.
        return error != _ERROR_INVALID_PARAMETER
    try:
        code = c_uint32()
        kernel32.GetExitCodeProcess.argtypes = (
            c_void_p,
            getattr(ctypes_module, "POINTER")(c_uint32),
        )
        if not kernel32.GetExitCodeProcess(handle, getattr(ctypes_module, "byref")(code)):
            return True
        return bool(code.value == _STILL_ACTIVE)
    finally:
        kernel32.CloseHandle.argtypes = (c_void_p,)
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` names a running process; unknown means alive (fail open).

    Windows is detected by capability, ``ctypes.WinDLL``, and probed with
    OpenProcess/GetExitCodeProcess: there ``os.kill(pid, 0)`` would terminate
    the process. Elsewhere signal 0 only checks existence.
    """

    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        import ctypes
    except Exception:
        return True
    windll = getattr(ctypes, "WinDLL", None)
    try:
        if windll is not None:
            return _windows_pid_alive(ctypes, windll, pid)
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:
        return True
    return True


def _heartbeat_live(path: Path | None, now: datetime) -> bool:
    """A heartbeat is live when it parses, is younger than 3x its poll interval
    and names a running process. Anything else counts as no watcher."""

    if path is None:
        return False
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_HEARTBEAT_MAX_BYTES + 1)
        if len(raw) > _HEARTBEAT_MAX_BYTES:
            return False
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    pid = data.get("pid")
    interval = data.get("poll_interval")
    updated_at = data.get("updated_at")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return False
    if isinstance(interval, bool) or not isinstance(interval, (int, float)):
        return False
    if not 0 < float(interval) <= _HEARTBEAT_MAX_INTERVAL:
        return False
    if not isinstance(updated_at, str):
        return False
    try:
        stamp = datetime.fromisoformat(updated_at.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    if (now - stamp).total_seconds() > HEARTBEAT_STALE_FACTOR * float(interval):
        return False
    return _pid_alive(pid)


def _card_origin(card_json: object) -> str | None:
    if not isinstance(card_json, str) or len(card_json) > _CARD_JSON_MAX_CHARS:
        return None
    try:
        card = json.loads(card_json)
    except ValueError:
        return None
    origin = card.get("origin_thread_id") if isinstance(card, dict) else None
    return origin if isinstance(origin, str) else None


def _session_tasks(
    conn: sqlite3.Connection, session_id: str
) -> tuple[list[str], list[str]] | None:
    """(review task ids, processing task ids) this session dispatched.

    Reads only rows the ``status`` index selects; ``card_json`` is parsed only
    for a row whose ``origin_thread_id`` column is empty. None: not a task
    queue this guard understands.
    """

    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")}
    if not _TASK_COLUMNS_REQUIRED <= columns:
        return None
    statuses = STOP_REVIEW_STATUSES + STOP_PROCESSING_STATUSES
    params: list[str] = list(statuses)
    if "origin_thread_id" in columns:
        select = (
            "origin_thread_id, CASE WHEN COALESCE(origin_thread_id, '') = '' "
            "THEN card_json END"
        )
        session_filter = " AND (origin_thread_id = ? OR COALESCE(origin_thread_id, '') = '')"
        params.append(session_id)
    else:
        select = "NULL, card_json"
        session_filter = ""
    archived = " AND COALESCE(archived_at, '') = ''" if "archived_at" in columns else ""
    sql = (
        f"SELECT task_id, status, {select} FROM tasks "
        f"WHERE status IN ({','.join('?' * len(statuses))}){session_filter}{archived} "
        "ORDER BY task_id"
    )
    review: list[str] = []
    processing: list[str] = []
    for task_id, status, origin, card_json in conn.execute(sql, params):
        owner = origin if isinstance(origin, str) and origin else _card_origin(card_json)
        if owner != session_id:
            continue
        bucket = review if str(status) in STOP_REVIEW_STATUSES else processing
        bucket.append(str(task_id))
    return review, processing


def _shell_arg(value: str) -> str:
    if _PLAIN_SHELL_ARG.match(value):
        return value
    return shlex.quote(value)


def _listed(task_ids: list[str]) -> str:
    shown = ", ".join(_clip(task_id, 80) for task_id in task_ids[:STOP_LISTED_TASKS_MAX])
    extra = len(task_ids) - STOP_LISTED_TASKS_MAX
    return shown + (f" (+{extra} more)" if extra > 0 else "")


def manager_watch_command(root: str | Path, session_id: str) -> str:
    """The exact watcher command the Stop guard asks the manager to start."""

    project = Path(root)
    python = "python"
    for candidate in (
        project / ".venv" / "Scripts" / "python.exe",
        project / ".venv" / "bin" / "python",
    ):
        if candidate.is_file():
            python = candidate.as_posix()
            break
    return " ".join(
        (
            _shell_arg(python),
            "-m aiworkhub.manager_callback_watch",
            "--repo",
            _shell_arg(project.as_posix()),
            "--provider claude",
            "--origin-thread-id",
            _shell_arg(session_id),
            "--timeout 86400",
        )
    )


def stop_decision(payload: object, root: str | Path) -> str | None:
    """The block reason for a manager Stop, or None to allow it.

    Fails open: ``stop_hook_active``, no session id, a missing database, a
    schema this guard does not know and any SQLite or OS error allow the stop.
    """

    if not isinstance(payload, dict) or payload.get("stop_hook_active"):
        return None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    db_path = Path(root).joinpath(*TASK_QUEUE_DB_PARTS)
    try:
        if not db_path.is_file():
            return None
        conn = _connect_readonly_db(db_path, _TASK_QUEUE_BUSY_SECONDS)
        try:
            tasks = _session_tasks(conn, session_id)
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return None
    if tasks is None:
        return None
    review, processing = tasks
    if review:
        reason = (
            f"AIWorkHub: {len(review)} card(s) this session dispatched await your review "
            f"decision: {_listed(review)}. Claim them with aiworkhub_claude_callback_wait, "
            "decide each card, then ack the callback before stopping."
        )
        return _clip(reason, STOP_REASON_MAX_CHARS)
    if not processing:
        return None
    heartbeat = manager_watch_heartbeat_path(root, session_id)
    if _heartbeat_live(heartbeat, datetime.now(timezone.utc)):
        return None
    reason = (
        f"AIWorkHub: card(s) this session dispatched are processing ({_listed(processing)}) "
        "and no live callback watcher wakes you when they finish. Start it in the "
        f"background, then stop: {manager_watch_command(root, session_id)}"
    )
    return _clip(reason, STOP_REASON_MAX_CHARS)


def _stop_main() -> int:
    """``--stop``: print one Stop-hook block decision or nothing; always 0."""

    try:
        payload = _read_payload()
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        root = os.environ.get("CLAUDE_PROJECT_DIR") or (cwd if isinstance(cwd, str) else "")
        reason = stop_decision(payload, root or os.getcwd())
        if reason is None:
            return 0
        sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
        sys.stdout.flush()
    except Exception:
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    """Hook entry point. Always returns 0 so a guard failure never blocks a tool call."""

    args = sys.argv[1:] if argv is None else list(argv)
    try:
        if "--reminder" in args:
            sys.stdout.write(REMINDER + "\nToolSearch " + "select:" + ",".join(MANAGER_TOOLSEARCH_SELECT_NAMES) + "\n")
            sys.stdout.flush()
            return 0
        if "--stop" in args:
            return _stop_main()
        payload = _read_payload()
        if not isinstance(payload, dict):
            return 0
        root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        tool_name = payload.get("tool_name")
        verdict = _evaluate(
            tool_name,
            payload.get("tool_input"),
            root,
            payload.get("cwd"),
            session_id=payload.get("session_id"),
        )
        if verdict is None:
            return 0
        rule, reason, target = verdict
        _record_denial(root, rule, tool_name, target, payload.get("session_id"))
        decision = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        sys.stdout.write(json.dumps(decision) + "\n")
        sys.stdout.flush()
    except Exception:
        return 0
    return 0


__all__ = [
    "DEFAULT_EXCLUDE_GLOBS",
    "EXCLUDED_DIRS",
    "HEARTBEAT_STALE_FACTOR",
    "INDEXED_SUFFIXES",
    "IndexPolicy",
    "MANAGER_WATCH_PARTS",
    "REASON_MAX_CHARS",
    "REMINDER",
    "REMINDER_MAX_BYTES",
    "SCRATCH_DIR",
    "SOURCE_GRAPH_DB_PARTS",
    "STOP_LISTED_TASKS_MAX",
    "STOP_PROCESSING_STATUSES",
    "STOP_REASON_MAX_CHARS",
    "STOP_REVIEW_STATUSES",
    "SourceGraphMembership",
    "TASK_QUEUE_DB_PARTS",
    "UNINDEXED_STATUSES",
    "decide",
    "glob_ignored",
    "index_admits",
    "load_index_policy",
    "main",
    "manager_watch_command",
    "manager_watch_heartbeat_path",
    "stop_decision",
    "under_include_root",
]


if __name__ == "__main__":
    raise SystemExit(main())
