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
* R2 recursive listing (find, fd, tree, ls -R, Get-ChildItem -Recurse, dir /s)
  of an indexed directory.
* R3 raw read (cat, head, sed, Get-Content, ...) of an indexed file.
* R4 inline python/node code that reads an indexed file or lists an indexed
  directory.
* R5 Read of an indexed file without a limit or with a limit above 400.
* R6 Edit of an existing repository file.
* R7 Write over an existing repository file that keeps most of its lines.

R6 and R7 leave ``.git`` and ``.aiworkhub`` alone: VCS and runtime state is not
source the semantic editor can take over.
"""

from __future__ import annotations

import base64
import difflib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

READ_LIMIT_MAX = 400
WRITE_SIMILARITY_DENY = 0.5
REASON_MAX_CHARS = 400
REMINDER_MAX_BYTES = 800
EXCLUDED_DIRS = frozenset(
    {".git", ".aiworkhub", ".venv", "node_modules", "third_party", "dist", "build"}
)
INDEXED_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".md", ".toml",
        ".yml", ".yaml", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".java",
        ".ps1", ".sh", ".css", ".html",
    }
)
_EDIT_EXEMPT_DIRS = frozenset({".git", ".aiworkhub"})

_DENIALS_PARTS = (".aiworkhub", "runtime", "tool_guard", "denials.jsonl")
_DENIALS_ROTATE_BYTES = 4 * 1024 * 1024
_STDIN_MAX_BYTES = 32 * 1024 * 1024
_COMMAND_MAX_CHARS = 1024 * 1024
_COMPARE_MAX_CHARS = 4 * 1024 * 1024
_LITERALS_MAX = 256
_NESTING_MAX = 3

_SOURCE_GRAPH = "aiworkhub_manager_source_graph_query / aiworkhub_worker_source_graph_query"
_UNINDEXED = " A bounded Read is allowed when Source Graph reports the target unindexed."
_READ_INSTEAD = (
    "Use " + _SOURCE_GRAPH + ' mode="body" or mode="slice", then Read offset+limit<=400'
    " from the returned range."
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
    "R5": (
        "AIWorkHub tool guard R5: Read of indexed file {target} needs a limit<=400. "
        + _READ_INSTEAD
    ),
    "R6": (
        "AIWorkHub tool guard R6: raw edit of an existing repository file is denied. "
        + _EDIT_INSTEAD
    ),
    "R7": (
        "AIWorkHub tool guard R7: this Write keeps {ratio} of an existing file; Write is"
        " for new or mostly rewritten files. " + _EDIT_INSTEAD
    ),
}
REMINDER = (
    "AIWorkHub tool guard (PreToolUse hook) is active for indexed source:\n"
    "- Search: aiworkhub_manager_source_graph_query or aiworkhub_worker_source_graph_query"
    ' mode="bodygrep", not grep/rg/findstr/Select-String/git grep.\n'
    '- Listing: mode="slice", not find/fd/tree/ls -R/Get-ChildItem -Recurse/dir /s.\n'
    '- Reading: mode="body" or "slice", then Read offset+limit<=400 from the returned'
    " range; not cat/head/sed/Get-Content or python/node file reads.\n"
    "- Editing an existing file: aiworkhub_worker_semantic_edit_prepare then"
    " aiworkhub_worker_semantic_edit_apply (worker) or scripts/manager_semantic_edit.py"
    " --path --start --end (manager); Write only new or mostly rewritten files.\n"
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

    __slots__ = ("words", "piped", "stdin", "inputs")

    def __init__(self, piped: bool) -> None:
        self.words: list[str] = []
        self.piped = piped
        self.stdin: str | None = None
        self.inputs: list[str] = []

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


class _Context:
    """Resolve operands against the repository root and the current directory."""

    def __init__(self, root: Path, cwd: Path, dialect: str) -> None:
        self.root = root
        self.cwd = cwd
        self.dialect = dialect

    def fork(self, dialect: str | None = None) -> _Context:
        return _Context(self.root, self.cwd, dialect or self.dialect)

    def moved(self, operand: str) -> _Context:
        scope = self.fork()
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
            candidate = (base or self.cwd) / candidate
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

    def indexed_rel(self, path: Path | None) -> str | None:
        parts = self.inside(path)
        if parts is None or any(part.lower() in EXCLUDED_DIRS for part in parts):
            return None
        return "/".join(parts) or "."

    def indexed_file(self, operand: str, glob: bool = True) -> str | None:
        if glob and _GLOB.search(operand):
            return self.indexed_glob(operand)
        path = self.path(operand)
        rel = self.indexed_rel(path)
        if path is None or rel is None or rel == ".":
            return None
        return rel if path.suffix.lower() in INDEXED_SUFFIXES else None

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
        if base is None or rel is None or any(part.lower() in EXCLUDED_DIRS for part in tail):
            return None
        suffix = _SUFFIX.search(tail[-1])
        if suffix is not None:
            if "." + suffix.group(1).lower() not in INDEXED_SUFFIXES:
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
    if name in ("grep", "egrep", "fgrep"):
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
    ctx: _Context, segment: _Segment, terms: tuple[str, list[str], bool]
) -> tuple[str, str, str] | None:
    pattern, operands, recursive = terms
    explicit = [operand for operand in operands + segment.inputs if operand]
    for operand in explicit:
        target = ctx.indexed(operand) if operand != "-" else None
        if target:
            return ("R1", target, pattern)
    # A pipe consumer with no file operand searches its stdin, not the tree.
    if explicit or segment.piped or segment.stdin is not None or not recursive:
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
            if path is not None and path.suffix.lower() in INDEXED_SUFFIXES and path.is_file():
                target = ctx.indexed_rel(path)
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
        return None if terms is None else _search_verdict(ctx, segment, terms)
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


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _similarity(previous: str, content: str) -> float:
    matcher = difflib.SequenceMatcher(None, previous.splitlines(), content.splitlines())
    # Both quick ratios bound ratio() from above, so a low one already decides.
    for bound in (matcher.real_quick_ratio, matcher.quick_ratio):
        ceiling = bound()
        if ceiling < WRITE_SIMILARITY_DENY:
            return ceiling
    return matcher.ratio()


def _write_verdict(ctx: _Context, file_path: str, content: object) -> tuple[str, str, str] | None:
    if not isinstance(content, str) or len(content) > _COMPARE_MAX_CHARS:
        return None
    path = ctx.path(file_path)
    target = ctx.editable(path)
    if path is None or target is None:
        return None
    try:
        if path.stat().st_size > _COMPARE_MAX_CHARS:
            return None
        previous = path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    ratio = _similarity(previous, content)
    if ratio < WRITE_SIMILARITY_DENY:
        return None
    return ("R7", target, "%.2f" % ratio)


def _tool_verdict(
    ctx: _Context, tool_name: str, tool_input: dict[str, object]
) -> tuple[str, str, str] | None:
    if tool_name in ("Bash", "PowerShell"):
        command = tool_input.get("command")
        return _shell_verdict(ctx, command) if isinstance(command, str) else None
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        return None
    if tool_name == "Read":
        target = ctx.indexed_file(file_path, glob=False)
        limit = _integer(tool_input.get("limit"))
        if target is None or (limit is not None and 0 < limit <= READ_LIMIT_MAX):
            return None
        return ("R5", target, "")
    if tool_name in ("Edit", "MultiEdit"):
        target = ctx.editable(ctx.path(file_path))
        return None if target is None else ("R6", target, "")
    if tool_name == "Write":
        return _write_verdict(ctx, file_path, tool_input.get("content"))
    return None


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: max(0, limit - 3)] + "..."


def _quoted(value: str, limit: int) -> str:
    body = json.dumps(value, ensure_ascii=False)[1:-1]
    return '"' + _clip(body, max(3, limit - 2)) + '"'


def _reason(rule: str, target: str, detail: str) -> str:
    template = _REASONS[rule] + _UNINDEXED
    ratio = detail if rule == "R7" else ""
    pattern = _CONTROL.sub(" ", detail).strip() if rule == "R1" else ""
    fixed = len(template.format(target="", pattern='""', ratio=ratio))
    slots = template.count("{target}") + template.count("{pattern}")
    share = max(16, (REASON_MAX_CHARS - fixed) // max(1, slots))
    text = template.format(
        target=_clip(_CONTROL.sub(" ", target).strip() or ".", share),
        pattern=_quoted(pattern, share),
        ratio=ratio,
    )
    return _clip(text, REASON_MAX_CHARS)


def _evaluate(
    tool_name: object, tool_input: object, root: str | os.PathLike[str], cwd: object = None
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
    verdict = _tool_verdict(_Context(base, where, dialect), tool_name, tool_input)
    if verdict is None:
        return None
    rule, target, detail = verdict
    return rule, _reason(rule, target, detail), target


def decide(
    tool_name: object, tool_input: object, root: str | os.PathLike[str], cwd: object = None
) -> tuple[str, str] | None:
    """Return ``(rule_id, reason)`` when the tool call must be denied, else None.

    Pure: it reads the tree under ``root`` but writes nothing. ``root`` is the
    project directory, and a root without ``.aiworkhub/`` allows everything.
    ``cwd`` (default ``root``) anchors relative shell operands.
    """

    verdict = _evaluate(tool_name, tool_input, root, cwd)
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


def main(argv: list[str] | None = None) -> int:
    """Hook entry point. Always returns 0 so a guard failure never blocks a tool call."""

    args = sys.argv[1:] if argv is None else list(argv)
    try:
        if "--reminder" in args:
            sys.stdout.write(REMINDER + "\n")
            sys.stdout.flush()
            return 0
        payload = _read_payload()
        if not isinstance(payload, dict):
            return 0
        root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        tool_name = payload.get("tool_name")
        verdict = _evaluate(tool_name, payload.get("tool_input"), root, payload.get("cwd"))
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
    "EXCLUDED_DIRS",
    "INDEXED_SUFFIXES",
    "READ_LIMIT_MAX",
    "REASON_MAX_CHARS",
    "REMINDER",
    "REMINDER_MAX_BYTES",
    "WRITE_SIMILARITY_DENY",
    "decide",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
