"""Quality-review source evidence builder, moved out of ``process_launcher``.

``ProcessManager._quality_review_source_evidence`` is bound to this function
unchanged; the byte budgets it reads stay class attributes on ``ProcessManager``.
"""

from __future__ import annotations

import ast
import difflib
import json
from pathlib import Path
from typing import Any, Mapping

from . import quality_reviewer
from .worker_workspace import WorkspaceError

# Bounded widening of a hunk's leading context up to its enclosing def/class
# header (NF-2026-00938); a header farther away is a typed omission.
_ENCLOSING_SYMBOL_MAX_CONTEXT_LINES = 40


def _python_symbol_spans(path: str, text: str) -> list[tuple[int, int]]:
    """1-based ``(header_line, end_line)`` of every def/class in a Python file.

    Non-Python or unparsable candidates have no spans, so their context stays
    the fixed line count (no enclosing-symbol claim is made for them).
    """
    if not path.endswith((".py", ".pyi")):
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        # A pathologically nested candidate can exhaust the parser's stack
        # or memory; it gets no spans instead of failing the whole review.
        return []
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            header = min([node.lineno] + [item.lineno for item in node.decorator_list])
            spans.append((header, int(node.end_lineno or node.lineno)))
    return spans


def _enclosing_symbol_lead(spans: list[tuple[int, int]], new_start: int) -> int:
    """Lines of leading context reaching the innermost enclosing header.

    ``new_start`` is the 0-based first changed candidate line; 0 means no
    symbol encloses the hunk with its header strictly before it.
    """
    first_changed = new_start + 1
    best = 0
    for header, end in spans:
        if header < first_changed <= end and header > best:
            best = header
    return first_changed - best if best else 0


def quality_review_source_evidence(
    self, workspace: Any, changed_hashes: Mapping[str, str | None]
) -> dict[str, dict[str, Any]]:
    """Build the complete unified diff of each changed path, candidate vs canonical.

    One hunk per non-equal SequenceMatcher opcode: an ``@@`` header naming
    the exact candidate and baseline line ranges, then ``' '`` context
    lines (bounded by the neighbouring equal blocks, so context is never
    another hunk's changed line), ``'-'`` baseline lines removed and
    ``'+'`` candidate lines added.  Hunks are emitted whole while the
    per-path and total byte budgets allow; a hunk that does not fit is cut
    at a line boundary and counted, and every cut or omitted hunk keeps
    its exact ``segments`` row so ``diff_complete`` is false only when
    something is missing and never silently.  A path with more hunks than
    ``quality_reviewer.MAX_SOURCE_EVIDENCE_SEGMENTS`` folds every hunk from
    the last slot onward into one truncated segment spanning them all.
    """

    def decode_utf8(raw: bytes) -> str | None:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def with_newline(line: str) -> str:
        return line if line.endswith("\n") else line + "\n"

    context_lines = self._QUALITY_REVIEW_SOURCE_CONTEXT_LINES
    evidence: dict[str, dict[str, Any]] = {}
    # Phase 1: read every path through the validated read (symlinks and
    # non-files are refused by name before anything else touches them) and
    # plan its hunks, measuring the bytes its complete excerpt needs.
    plans: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    needs: dict[str, int] = {}
    for path in sorted(changed_hashes):
        candidate = Path(workspace.path) / path
        baseline = Path(self.repo) / path
        row: dict[str, Any] = {
            "candidate_sha256": changed_hashes[path],
            "excerpt": "",
            "excerpt_bytes": 0,
            "source_bytes": 0,
            "truncated": False,
            "diff_complete": False,
            "segments": [],
        }
        evidence[path] = row
        try:
            if candidate.is_symlink():
                raise WorkspaceError(
                    f"quality_review_candidate_unreadable:{path}"
                )
            if not candidate.is_file():
                if changed_hashes[path] is None:
                    row["omission_reason"] = "candidate_deleted_or_non_file"
                    continue
                raise WorkspaceError(
                    f"quality_review_candidate_unreadable:{path}"
                )
            candidate_bytes = candidate.read_bytes()
            baseline_bytes = baseline.read_bytes() if baseline.is_file() else b""
        except OSError as exc:
            raise WorkspaceError(
                f"quality_review_candidate_unreadable:{path}"
            ) from exc
        row["source_bytes"] = len(candidate_bytes)
        candidate_text = decode_utf8(candidate_bytes)
        baseline_text = decode_utf8(baseline_bytes)
        if candidate_text is None:
            row["omission_reason"] = "candidate_non_utf8"
            row["truncated"] = True
            continue
        if baseline_text is None:
            baseline_text = ""
            row["baseline_omission_reason"] = "baseline_non_utf8"
        candidate_lines = candidate_text.splitlines(keepends=True)
        baseline_lines = baseline_text.splitlines(keepends=True)
        if not candidate_lines and candidate_text:
            candidate_lines = [candidate_text]
        opcodes = difflib.SequenceMatcher(
            None, baseline_lines, candidate_lines
        ).get_opcodes()
        header_path = json.dumps(path, ensure_ascii=True)
        # The reviewer refuses a row carrying more than
        # MAX_SOURCE_EVIDENCE_SEGMENTS segments, so every hunk from the
        # last slot onward folds into ONE truncated segment spanning them.
        segment_cap = quality_reviewer.MAX_SOURCE_EVIDENCE_SEGMENTS
        hunk_indexes = [
            index for index, opcode in enumerate(opcodes) if opcode[0] != "equal"
        ]
        fold_from = (
            hunk_indexes[segment_cap - 1]
            if len(hunk_indexes) > segment_cap
            else len(opcodes)
        )
        symbol_spans = _python_symbol_spans(path, candidate_text)
        hunks: list[dict[str, Any]] = []
        need = 0
        for index, (tag, old_start, old_end, new_start, new_end) in enumerate(opcodes):
            if tag == "equal":
                continue
            # Context is bounded by the adjacent EQUAL blocks: a short
            # equal run between two hunks must not let one hunk's context
            # swallow the other hunk's changed lines as if unchanged.
            before = 0
            far_header_line: int | None = None
            if index > 0 and opcodes[index - 1][0] == "equal":
                before = min(context_lines, opcodes[index - 1][4] - opcodes[index - 1][3])
                # Widen the leading context to the enclosing symbol's header
                # (NF-2026-00938) so the reviewer sees which def/class the
                # hunk changes; a header past the bounded widening is named
                # as a typed omission unless an earlier excerpt of the same
                # path already shows it.
                needed = _enclosing_symbol_lead(symbol_spans, new_start)
                if needed > before:
                    available = opcodes[index - 1][4] - opcodes[index - 1][3]
                    if needed > _ENCLOSING_SYMBOL_MAX_CONTEXT_LINES:
                        far_header_line = new_start + 1 - needed
                    elif needed <= available:
                        before = needed
            after = 0
            if index + 1 < len(opcodes) and opcodes[index + 1][0] == "equal":
                after = min(context_lines, opcodes[index + 1][4] - opcodes[index + 1][3])
            start_line = new_start - before + 1
            end_line = max(start_line, new_end + after)
            segment = {
                "kind": tag,
                "candidate_start_line": start_line,
                "candidate_end_line": end_line,
                "changed_start_line": new_start + 1,
                "changed_end_line": max(new_start + 1, new_end),
                "baseline_start_line": old_start + 1,
                "baseline_end_line": max(old_start + 1, old_end),
                "excerpt_bytes": 0,
                "truncated": True,
            }
            hunk: dict[str, Any] = {
                "segment": segment,
                "far_header_line": far_header_line,
                "folded": index >= fold_from,
            }
            hunks.append(hunk)
            if hunk["folded"]:
                continue
            header = (
                f"@@ path:{header_path} candidate:{start_line}-{end_line} "
                f"change:{new_start + 1}-{max(new_start + 1, new_end)} "
                f"baseline:{old_start + 1}-{max(old_start + 1, old_end)} {tag} @@\n"
            )
            # Each body line carries its 1-based candidate line number (None
            # for a removed baseline line) so emitted coverage is exact.
            body_lines: list[tuple[str, int | None]] = (
                [
                    (" " + with_newline(line), new_start - before + 1 + offset)
                    for offset, line in enumerate(candidate_lines[new_start - before:new_start])
                ]
                + [("-" + with_newline(line), None) for line in baseline_lines[old_start:old_end]]
                + [
                    ("+" + with_newline(line), new_start + 1 + offset)
                    for offset, line in enumerate(candidate_lines[new_start:new_end])
                ]
                + [
                    (" " + with_newline(line), new_end + 1 + offset)
                    for offset, line in enumerate(candidate_lines[new_end:new_end + after])
                ]
            )
            body_sizes = [len(line.encode("utf-8")) for line, _ in body_lines]
            hunk["header"] = header
            hunk["header_bytes"] = len(header.encode("utf-8"))
            hunk["body"] = body_lines
            hunk["body_sizes"] = body_sizes
            need += hunk["header_bytes"] + sum(body_sizes)
        plans[path] = (row, hunks)
        needs[path] = need

    # Phase 2 (NF-2026-00937): water-fill the shared total over the measured
    # needs, smallest first with the path name breaking ties.  Each path may
    # take at most an even split of what is left across the paths still to
    # come, so a path needing less than its share leaves the rest to larger
    # ones and no path's place in the alphabet decides who is starved.
    remaining = self._QUALITY_REVIEW_SOURCE_TOTAL_MAX_BYTES
    ordered_paths = sorted(plans, key=lambda name: (needs[name], name))
    for position, path in enumerate(ordered_paths):
        row, hunks = plans[path]
        path_remaining = min(
            self._QUALITY_REVIEW_SOURCE_MAX_BYTES,
            max(0, remaining) // max(1, len(ordered_paths) - position),
        )
        chunks: list[str] = []
        omitted_hunks = 0
        folded: dict[str, Any] | None = None
        # 1-based candidate lines already shown by an emitted excerpt of
        # this path: a far enclosing header visible there is not omitted.
        emitted_lines: set[int] = set()
        for hunk in hunks:
            segment = hunk["segment"]
            far_header_line = hunk["far_header_line"]
            if far_header_line is not None and far_header_line not in emitted_lines:
                row["enclosing_symbol_context_omitted"] = (
                    int(row.get("enclosing_symbol_context_omitted") or 0) + 1
                )
            if hunk["folded"]:
                if folded is None:
                    folded = segment
                else:
                    if folded["kind"] != segment["kind"]:
                        folded["kind"] = "replace"
                    folded["candidate_end_line"] = segment["candidate_end_line"]
                    folded["changed_end_line"] = segment["changed_end_line"]
                    folded["baseline_end_line"] = segment["baseline_end_line"]
                continue
            limit = max(0, min(path_remaining, remaining))
            header_bytes = hunk["header_bytes"]
            if limit <= header_bytes:
                omitted_hunks += 1
                row["segments"].append(segment)
                continue
            budget = limit - header_bytes
            emitted: list[str] = []
            used = 0
            truncated = False
            for (line, line_number), encoded in zip(hunk["body"], hunk["body_sizes"]):
                if used + encoded > budget:
                    truncated = True
                    break
                emitted.append(line)
                used += encoded
                if line_number is not None:
                    emitted_lines.add(line_number)
            segment_bytes = header_bytes + used
            remaining -= segment_bytes
            path_remaining -= segment_bytes
            row["excerpt_bytes"] += segment_bytes
            segment["excerpt_bytes"] = segment_bytes
            segment["truncated"] = truncated
            row["segments"].append(segment)
            chunks.append(hunk["header"] + "".join(emitted))
            if truncated:
                omitted_hunks += 1
        if folded is not None:
            # Counted once: the folded segment is the one truncated row
            # that stands for every hunk past the reviewer's bound.
            omitted_hunks += 1
            row["segments"].append(folded)
        row["diff_complete"] = omitted_hunks == 0
        row["truncated"] = omitted_hunks > 0
        if omitted_hunks:
            row["omission_reason"] = f"changed_hunks_omitted:{omitted_hunks}"
        if not chunks and "omission_reason" not in row:
            row["omission_reason"] = "empty_diff"
        row["excerpt"] = "".join(chunks)
    return {path: evidence[path] for path in sorted(evidence)}
