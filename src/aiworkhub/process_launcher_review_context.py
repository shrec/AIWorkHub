"""Quality-review caller context for one review, extracted from ``ProcessManager``.

``ProcessManager._quality_review_caller_context`` is a thin delegation to
:func:`quality_review_caller_context` below (NF-2026-01773: ``process_launcher.py``
sat above its module size ratchet).  The move is byte-for-byte apart from the
four-space dedent a method-to-function move forces; ``self`` is still the
:class:`ProcessManager` instance, so ``self.repo`` and the four
``_QUALITY_REVIEW_CALLER_CONTEXT_*`` class constants resolve exactly as before.
Module level collaborators are re-bound from the ``process_launcher`` module
object on entry rather than imported here, because the test suite monkeypatches
that module and a name captured at import time would leave those patches
pointing at code this function no longer calls -- silently, with the tests
still green.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

__all__ = ["REVIEW_CONTEXT_SEAM_NAMES", "quality_review_caller_context"]

# The function's complete runtime free-variable set on ``process_launcher``.
# ``Any`` appears only in annotations, which are never evaluated, so it is
# resolved from this module's own import and is deliberately NOT re-bound.
REVIEW_CONTEXT_SEAM_NAMES: tuple[str, ...] = (
    "Mapping",
    "Path",
)


def quality_review_caller_context(
    self,
    changed_hashes: Mapping[str, str | None],
    scoped_audits: Mapping[str, Mapping[str, Any]],
    *,
    candidate_root: Path | str | None = None,
) -> dict[str, Any]:
    """Read the source around every graph-resolved caller line.

    The rows come from the scoped audit's ``impact_evidence`` of kind
    ``callers`` -- Source Graph edges into the changed symbols.  They are
    lens-independent, so one lens's rows are read.  A caller outside the
    changed paths is read from the canonical tree (``source: canonical``).  A
    caller that lives in a changed path is read from ``candidate_root`` under
    the same guards and caps (``source: candidate``): when every caller of a
    changed symbol sits in the changed file the diff is not a substitute for
    the graph line.  Nothing is model prose, and no caller is dropped
    silently: a malformed entry, a missing ``candidate_root``, unreadable
    candidate bytes and cap overflow all count in ``omitted``.
    """
    # Every module-level name this function used while it lived in
    # ``process_launcher`` is re-bound here from that exact module object, so a
    # ``monkeypatch.setattr(process_launcher, name, ...)`` seam still reaches the
    # code below.  The list is ``REVIEW_CONTEXT_SEAM_NAMES`` and is asserted
    # against the body by ``test_process_launcher_extraction_seams_nf01729.py``.
    from . import process_launcher as _pl

    Mapping = _pl.Mapping
    Path = _pl.Path

    def with_newline(line: str) -> str:
        return line if line.endswith("\n") else line + "\n"

    callers: dict[str, tuple[str, int]] = {}
    omitted = 0
    for lens in sorted(scoped_audits):
        wrapper = scoped_audits[lens]
        scope = wrapper.get("packet") if isinstance(wrapper, Mapping) else None
        if not isinstance(scope, Mapping):
            continue
        for entry in scope.get("impact_evidence") or []:
            if not isinstance(entry, Mapping) or entry.get("evidence_kind") != "callers":
                continue
            identity = str(entry.get("identity") or "")
            path = str(entry.get("path") or "")
            line = entry.get("line_start")
            if (
                not identity
                or not path
                or path.startswith("/")
                or ".." in path.split("/")
                or type(line) is not int
                or line < 1
            ):
                # A malformed callers entry is a caller the packet could not
                # carry: count it so ``complete`` cannot be true.
                omitted += 1
                continue
            callers.setdefault(identity, (path, line))
        break
    context = self._QUALITY_REVIEW_CALLER_CONTEXT_LINES
    rows: list[dict[str, Any]] = []
    remaining = self._QUALITY_REVIEW_CALLER_CONTEXT_TOTAL_MAX_BYTES
    cache: dict[tuple[str, str], list[str]] = {}
    ordered = sorted(callers, key=lambda key: (callers[key][0], callers[key][1], key))
    for identity in ordered:
        path, line = callers[identity]
        if len(rows) >= self._QUALITY_REVIEW_CALLER_CONTEXT_MAX_ROWS:
            omitted += 1
            continue
        origin = "candidate" if path in changed_hashes else "canonical"
        lines = cache.get((origin, path))
        if lines is None:
            root = candidate_root if origin == "candidate" else self.repo
            lines = []
            if root is not None:
                source = Path(root) / path
                try:
                    # resolve() follows every link component, not just the
                    # leaf, so a linked directory cannot lead outside root.
                    if (
                        not source.is_symlink()
                        and source.is_file()
                        and source.resolve().is_relative_to(Path(root).resolve())
                    ):
                        lines = source.read_bytes().decode("utf-8").splitlines(keepends=True)
                except (OSError, UnicodeDecodeError):
                    lines = []
            cache[(origin, path)] = lines
        if line > len(lines):
            omitted += 1
            continue
        start = max(1, line - context)
        end = min(len(lines), line + context)
        text = "".join(with_newline(item) for item in lines[start - 1:end])
        size = len(text.encode("utf-8"))
        if size > self._QUALITY_REVIEW_CALLER_CONTEXT_ROW_MAX_BYTES or size > remaining:
            omitted += 1
            continue
        remaining -= size
        rows.append(
            {
                "identity": identity,
                "path": path,
                "line": line,
                "line_start": start,
                "line_end": end,
                "source": origin,
                "text": text,
            }
        )
    return {"rows": rows, "complete": omitted == 0, "omitted": omitted}
