"""Quality-review source evidence builder, moved out of ``process_launcher``.

``ProcessManager._quality_review_source_evidence`` is bound to this function
unchanged; the byte budgets it reads stay class attributes on ``ProcessManager``.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any, Mapping

from . import quality_reviewer
from .worker_workspace import WorkspaceError


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
    remaining = self._QUALITY_REVIEW_SOURCE_TOTAL_MAX_BYTES
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
        chunks: list[str] = []
        omitted_hunks = 0
        path_remaining = self._QUALITY_REVIEW_SOURCE_MAX_BYTES
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
        folded: dict[str, Any] | None = None
        for index, (tag, old_start, old_end, new_start, new_end) in enumerate(opcodes):
            if tag == "equal":
                continue
            # Context is bounded by the adjacent EQUAL blocks: a short
            # equal run between two hunks must not let one hunk's context
            # swallow the other hunk's changed lines as if unchanged.
            before = 0
            if index > 0 and opcodes[index - 1][0] == "equal":
                before = min(context_lines, opcodes[index - 1][4] - opcodes[index - 1][3])
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
            if index >= fold_from:
                if folded is None:
                    folded = segment
                else:
                    if folded["kind"] != tag:
                        folded["kind"] = "replace"
                    folded["candidate_end_line"] = segment["candidate_end_line"]
                    folded["changed_end_line"] = segment["changed_end_line"]
                    folded["baseline_end_line"] = segment["baseline_end_line"]
                continue
            header = (
                f"@@ path:{header_path} candidate:{start_line}-{end_line} "
                f"change:{new_start + 1}-{max(new_start + 1, new_end)} "
                f"baseline:{old_start + 1}-{max(old_start + 1, old_end)} {tag} @@\n"
            )
            body_lines = (
                [" " + with_newline(line) for line in candidate_lines[new_start - before:new_start]]
                + ["-" + with_newline(line) for line in baseline_lines[old_start:old_end]]
                + ["+" + with_newline(line) for line in candidate_lines[new_start:new_end]]
                + [" " + with_newline(line) for line in candidate_lines[new_end:new_end + after]]
            )
            limit = max(0, min(path_remaining, remaining))
            header_bytes = len(header.encode("utf-8"))
            if limit <= header_bytes:
                omitted_hunks += 1
                row["segments"].append(segment)
                continue
            budget = limit - header_bytes
            emitted: list[str] = []
            used = 0
            truncated = False
            for line in body_lines:
                encoded = len(line.encode("utf-8"))
                if used + encoded > budget:
                    truncated = True
                    break
                emitted.append(line)
                used += encoded
            segment_bytes = header_bytes + used
            remaining -= segment_bytes
            path_remaining -= segment_bytes
            row["excerpt_bytes"] += segment_bytes
            segment["excerpt_bytes"] = segment_bytes
            segment["truncated"] = truncated
            row["segments"].append(segment)
            chunks.append(header + "".join(emitted))
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
    return evidence
