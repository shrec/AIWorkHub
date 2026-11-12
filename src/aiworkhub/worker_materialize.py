"""Card ``materialize`` globs and ``checkout`` mode for worker trees (NF-2026-02237).

A worker worktree is a sparse checkout of its declared closure.  A card may
widen what is present on disk -- never what it may write:

* ``materialize``: up to ``MAX_MATERIALIZE`` repo-relative globs added to the
  sparse-checkout pattern set (same path rules as ``allowed_writes``: no
  absolute path, no ``..``, no ``.git``).  The repo may add a default list
  under ``worker_materialize`` in ``.aiworkhub/config/project.json``.
* ``checkout``: ``"sparse"`` (default) or ``"full"`` (the whole ``HEAD`` tree).

Writes stay limited by ``allowed_writes``: materialized files are tracked
paths outside it, so the existing scope gate still rejects a change to them.
The card's ``forbidden`` paths are never materialized once either option is in
effect: they are emitted as negative sparse patterns after the positive ones,
and every tree is checked (and cleaned) for them afterwards, failing closed.

A card that uses neither option, in a repo without a default list, produces
exactly the sparse pattern list it produced before this module existed.  This
module is the one owner of that rule for the worker tree, the validation tree
and the reviewer reproduction tree.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_MATERIALIZE = 64
MAX_PATTERN_CHARS = 512
CHECKOUT_MODES = ("sparse", "full")
DEFAULT_CHECKOUT = "sparse"
PROJECT_CONFIG_RELATIVE = (".aiworkhub", "config", "project.json")
PROJECT_CONFIG_KEY = "worker_materialize"
MAX_PROJECT_CONFIG_BYTES = 1024 * 1024
MAX_FORBIDDEN_REMOVALS = 10000
EMPTY_SPARSE_PATTERN = "/__AIWORKHUB_EMPTY_SPARSE_WORKTREE__"
_DETAIL_CHARS = 240
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

RunGit = Callable[..., Any]
Failure = Callable[[str], BaseException]


class CardCheckoutError(ValueError):
    """A refused ``materialize``/``checkout`` value, with a stable reason code.

    ``str(exc)`` is ``<field>_invalid:<reason>:<detail>`` so task_create can
    return it verbatim as a typed lifecycle error.
    """

    def __init__(self, field: str, reason: str, detail: object = "") -> None:
        self.field = field
        self.reason = reason
        self.detail = str(detail)[:_DETAIL_CHARS]
        super().__init__(f"{field}_invalid:{reason}:{self.detail}")


def _check_glob(field: str, item: Any) -> str:
    if not isinstance(item, str):
        raise CardCheckoutError(field, "not_a_string", type(item).__name__)
    value = item.strip()
    if not value:
        raise CardCheckoutError(field, "empty")
    if len(value) > MAX_PATTERN_CHARS:
        raise CardCheckoutError(field, "too_long", f"{len(value)}>{MAX_PATTERN_CHARS}")
    if any(ch in value for ch in ("\x00", "\n", "\r")):
        raise CardCheckoutError(field, "control_character", value.encode("unicode_escape"))
    if "\\" in value:
        raise CardCheckoutError(field, "backslash", value)
    if value.startswith("!"):
        raise CardCheckoutError(field, "negation", value)
    if value.startswith("/") or _DRIVE_RE.match(value):
        raise CardCheckoutError(field, "absolute", value)
    parts = value.rstrip("/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise CardCheckoutError(field, "dot_or_empty_component", value)
    if any(part.casefold() == ".git" for part in parts):
        raise CardCheckoutError(field, "git_metadata", value)
    return value.rstrip("/")


def validate_materialize(raw: Any, *, field: str = "materialize") -> list[str]:
    """Normalized, order-preserving deduplicated globs; ``None`` is ``[]``."""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise CardCheckoutError(field, "not_a_list", type(raw).__name__)
    if len(raw) > MAX_MATERIALIZE:
        raise CardCheckoutError(field, "too_many", f"{len(raw)}>{MAX_MATERIALIZE}")
    return list(dict.fromkeys(_check_glob(field, item) for item in raw))


def validate_checkout(raw: Any) -> str:
    """``"sparse"`` or ``"full"``; ``None``/empty is the sparse default."""
    if raw is None or raw == "":
        return DEFAULT_CHECKOUT
    if not isinstance(raw, str) or raw.strip().lower() not in CHECKOUT_MODES:
        raise CardCheckoutError("checkout", "unknown_value", raw)
    return raw.strip().lower()


def validate_card_checkout(materialize: Any, checkout: Any) -> tuple[list[str], str]:
    """The task_create validator for both fields."""
    return validate_materialize(materialize), validate_checkout(checkout)


def card_checkout_fields(materialize: Sequence[str], checkout: str) -> dict[str, Any]:
    """Card keys to persist: only non-default values, so old cards stay identical."""
    fields: dict[str, Any] = {}
    if materialize:
        fields["materialize"] = list(materialize)
    if checkout != DEFAULT_CHECKOUT:
        fields["checkout"] = checkout
    return fields


def repo_default_materialize(repo: str | os.PathLike[str]) -> list[str]:
    """``worker_materialize`` from the repo's project config; absent is ``[]``.

    An unreadable, oversized or malformed config refuses (fail closed): an
    unknown default must not silently become "nothing extra".
    """
    config = Path(repo).joinpath(*PROJECT_CONFIG_RELATIVE)
    field = PROJECT_CONFIG_KEY
    if not config.exists():
        return []
    if config.is_symlink() or not config.is_file():
        raise CardCheckoutError(field, "config_not_regular", config.name)
    try:
        with config.open("rb") as handle:
            payload = handle.read(MAX_PROJECT_CONFIG_BYTES + 1)
    except OSError as exc:
        raise CardCheckoutError(field, "config_unreadable", type(exc).__name__) from exc
    if len(payload) > MAX_PROJECT_CONFIG_BYTES:
        raise CardCheckoutError(field, "config_too_large", len(payload))
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CardCheckoutError(field, "config_invalid_json", type(exc).__name__) from exc
    if not isinstance(data, dict):
        raise CardCheckoutError(field, "config_not_an_object", type(data).__name__)
    return validate_materialize(data.get(field), field=field)


def _forbidden_paths(raw: Any) -> tuple[str, ...]:
    """Path-like ``forbidden`` entries under the shared card path grammar."""
    if not raw:
        return ()
    from .core import _task_contract_path  # the one owner of the grammar

    paths: list[str] = []
    for item in raw if isinstance(raw, (list, tuple)) else [raw]:
        value = _task_contract_path(item).rstrip("/")
        if not value:
            continue  # prose constraint, not a path
        parts = value.split("/")
        if parts[0].casefold() == ".git":
            continue  # Git never checks out its own metadata
        if ".." in parts or any(ch in value for ch in ("\n", "\r")):
            raise CardCheckoutError("forbidden", "unexpressible_path", value)
        paths.append(value)
    return tuple(dict.fromkeys(paths))


def _matches(relative: str, pattern: str) -> bool:
    if fnmatch.fnmatchcase(relative, pattern):
        return True
    parts = relative.split("/")
    return any(
        fnmatch.fnmatchcase("/".join(parts[:index]), pattern)
        for index in range(1, len(parts))
    )


@dataclass(frozen=True)
class CheckoutPolicy:
    """The resolved materialize/checkout/forbidden rule for one card."""

    materialize: tuple[str, ...] = ()
    checkout: str = DEFAULT_CHECKOUT
    forbidden: tuple[str, ...] = ()

    @property
    def opted_in(self) -> bool:
        return bool(self.materialize) or self.checkout == "full"

    def negative_patterns(self) -> list[str]:
        return [f"!/{path}" for path in self.forbidden] if self.opted_in else []

    def sparse_patterns(self, base: Sequence[str]) -> list[str]:
        """The sparse-checkout pattern list for ``base`` exact/allowed patterns.

        Not opted in: ``base`` unchanged (or the empty sentinel) -- the
        byte-identical pre-NF-2026-02237 list.  Opted in: positives first,
        then one negative per forbidden path, so the forbidden ones win.
        """
        if not self.opted_in:
            return list(base) or [EMPTY_SPARSE_PATTERN]
        if self.checkout == "full":
            positives = ["/*"]
        else:
            positives = sorted(set(base) | {f"/{glob}" for glob in self.materialize})
        return (positives or [EMPTY_SPARSE_PATTERN]) + self.negative_patterns()

    def drop_forbidden(
        self, closure: tuple[list[str], tuple[str, ...], list[str]]
    ) -> tuple[list[str], tuple[str, ...], list[str]]:
        """A seed closure without forbidden paths (unchanged when not opted in)."""
        if not (self.opted_in and self.forbidden):
            return closure
        live, support, seeded = closure

        def keep(relative: str) -> bool:
            return not any(_matches(relative, pattern) for pattern in self.forbidden)

        return (
            [p for p in live if keep(p)],
            tuple(p for p in support if keep(p)),
            [p for p in seeded if keep(p)],
        )

    def remove_forbidden(self, path: Path, *, failure: Failure) -> list[str]:
        """Delete any forbidden path still present under ``path``; fail closed."""
        if not (self.opted_in and self.forbidden):
            return []
        root = Path(os.path.realpath(path))
        removed: list[str] = []
        for pattern in self.forbidden:
            try:
                # Materialized before any deletion: a lazy glob must not walk a tree it is removing.
                candidates: Iterable[Path] = (
                    list(path.glob(pattern))
                    if any(ch in pattern for ch in "*?[")
                    else [path / pattern]
                )
            except (OSError, ValueError) as exc:
                raise failure(
                    f"forbidden_glob_failed:{pattern[:_DETAIL_CHARS]}:{type(exc).__name__}"
                ) from exc
            for candidate in candidates:
                if not os.path.lexists(candidate):
                    continue
                relative = candidate.relative_to(path).as_posix()
                # Windows folds case and trailing dots/spaces: ".GIT" and ".git." name ".git".
                if relative.split("/")[0].lower().rstrip(". ") == ".git":
                    continue
                if len(removed) >= MAX_FORBIDDEN_REMOVALS:
                    raise failure(f"forbidden_removal_limit:{MAX_FORBIDDEN_REMOVALS}")
                parent = Path(os.path.realpath(candidate.parent))
                if parent != root and root not in parent.parents:
                    raise failure(f"forbidden_removal_escapes_tree:{relative}")
                try:
                    if candidate.is_dir() and not candidate.is_symlink():
                        shutil.rmtree(candidate)
                    else:
                        candidate.unlink()
                except OSError as exc:
                    raise failure(
                        f"forbidden_removal_failed:{relative}:{type(exc).__name__}"
                    ) from exc
                if os.path.lexists(candidate):
                    raise failure(f"forbidden_removal_incomplete:{relative}")
                removed.append(relative)
        return removed

    def expand_full_tree(
        self, path: Path, *, run: RunGit, timeout: float, failure: Failure
    ) -> None:
        """Materialize the whole ``HEAD`` tree minus forbidden paths.

        Without forbidden exclusions this is exactly ``git sparse-checkout
        disable`` (the established full-tree call).  With them, the sparse
        definition becomes ``/*`` plus the negatives, so the excluded files are
        skip-worktree and their absence is never reported as a deletion.
        ``failure(detail)`` builds the caller's own error; ``detail`` is the
        stderr tail of the failing Git step or a ``forbidden_removal_*`` cause.
        """
        negatives = self.negative_patterns()
        if negatives:
            text = "\n".join(["/*", *negatives]) + "\n"
            steps: list[tuple[list[str], str | None]] = [
                (["git", "sparse-checkout", "set", "--no-cone", "--stdin"], text),
                (["git", "read-tree", "-mu", "HEAD"], None),
            ]
        else:
            steps = [(["git", "sparse-checkout", "disable"], None)]
        for argv, input_text in steps:
            kwargs: dict[str, Any] = {
                "cwd": path, "timeout": timeout, "phase": "workspace_provision",
            }
            if input_text is not None:
                kwargs["input_text"] = input_text
            result = run(argv, **kwargs)
            if result.returncode != 0:
                raise failure(str(result.stderr or "")[-500:])
        self.remove_forbidden(path, failure=failure)

    def finish(self, path: Path, *, run: RunGit, timeout: float, failure: Failure) -> None:
        """Post-sparse step of worker provisioning: full expansion or cleanup."""
        if self.checkout == "full":
            self.expand_full_tree(path, run=run, timeout=timeout, failure=failure)
        else:
            self.remove_forbidden(path, failure=failure)


def checkout_policy(
    card: Mapping[str, Any],
    repo: str | os.PathLike[str],
    *,
    error: Callable[[str], BaseException] | None = None,
) -> CheckoutPolicy:
    """Re-validate the card fields (fail closed) and merge the repo default.

    ``error`` converts a :class:`CardCheckoutError` into the caller's own
    error type; omitted, the ``CardCheckoutError`` propagates.
    """
    try:
        card_globs, checkout = validate_card_checkout(
            card.get("materialize"), card.get("checkout")
        )
        globs = tuple(dict.fromkeys([*repo_default_materialize(repo), *card_globs]))
        if len(globs) > 2 * MAX_MATERIALIZE:
            raise CardCheckoutError("materialize", "too_many", len(globs))
        if not globs and checkout == DEFAULT_CHECKOUT:
            # Neither option in effect: the legacy rule, whose forbidden list
            # never shaped the tree, so it is not even parsed here.
            return CheckoutPolicy()
        return CheckoutPolicy(globs, checkout, _forbidden_paths(card.get("forbidden")))
    except CardCheckoutError as exc:
        if error is None:
            raise
        raise error(str(exc)) from exc


def expand_full_tree(
    path: Path,
    card: Mapping[str, Any],
    repo: str | os.PathLike[str],
    *,
    run: RunGit,
    timeout: float,
    failure: Failure,
) -> None:
    """Full-tree expansion for the validation and reproduction trees."""
    policy = checkout_policy(card, repo, error=failure)
    policy.expand_full_tree(path, run=run, timeout=timeout, failure=failure)
