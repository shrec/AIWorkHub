"""Deterministic foundation for RM-2026-00016 external repository qualification.

Phase boundary
--------------
This module is *foundation only*. It defines, validates and canonically
serializes two repository-owned artifacts: the heterogeneous external corpus
manifest, and the replayable qualification run artifact. Nothing here clones,
fetches, builds, mutates or executes an external repository, and no
performance, token or cost claim may be derived from this phase.

Corpus state
------------
The corpus is fixed and checked in: :func:`fixed_corpus_manifest` pins one
public repository per profile at a full-length commit object id, each read from
that repository's own remote ref advertisement for a release tag and recorded
as ``pin_evidence``. Reading ref metadata is not a clone, a build or an
execution, so the corpus is materialized without crossing the phase boundary.
:func:`validate_corpus_manifest` is the contract, and the checked-in manifest is
validated against it at import.

Acceptance authority
--------------------
Success means one thing: a manager acceptance carrying the canonical
``aiworkhub.accepted_outcome_receipt.v1`` identity, authenticated by
``task_engine._validate_accepted_outcome_receipt``. This module reuses that
authority instead of defining a second, weaker one, and it *requires* it: an
accepted row validated without a bound authority fails closed to
``success: False``, so no self-declared authentication block can stand in for
the real thing. A launched process, a worker that exited zero, a review that
became ready, and a validation command that passed are progress signals: they
are recorded, never projected onto success.

Missing evidence
----------------
An absent metric stays ``UNKNOWN``. It is never coerced to zero, and an
insufficient sample never becomes an inferred average.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import TYPE_CHECKING, Any

from .task_engine import ACCEPTED_OUTCOME_RECEIPT_SCHEMA, _canonical_json_hash
from .task_engine import _validate_accepted_outcome_receipt as _canonical_receipt_authority

if TYPE_CHECKING:
    from collections.abc import Callable

    # The canonical ``(receipt, refusal_reason)`` contract; an empty reason and a
    # non-None receipt is the only authentication result.
    AcceptanceAuthority = Callable[[dict[str, Any]], "tuple[dict[str, Any] | None, str]"]

# The exact field set ``task_engine._validate_accepted_outcome_receipt`` admits.
# Nothing in this module may admit a receipt that authority would reject.
ACCEPTED_OUTCOME_RECEIPT_FIELDS: frozenset[str] = frozenset({
    "schema_id",
    "receipt_id",
    "task_id",
    "request_id",
    "claim_epoch",
    "base_oid",
    "promoted_paths",
    "changed_path_hashes",
    "attempt_artifact_manifest_id",
    "repository_revision",
})

CORPUS_MANIFEST_SCHEMA = "aiworkhub.external_qualification.corpus_manifest.v1"
CORPUS_SPECIFICATION_SCHEMA = "aiworkhub.external_qualification.corpus_specification.v1"
RUN_ARTIFACT_SCHEMA = "aiworkhub.external_qualification.run_artifact.v1"
SUMMARY_SCHEMA = "aiworkhub.external_qualification.summary.v1"

PHASE_BOUNDARY = (
    "foundation_only: this phase validates and serializes qualification artifacts. "
    "No external repository is cloned, built or executed here, and no performance, "
    "token or cost claim is established yet."
)

CLAIM_BOUNDARY = (
    "Rows are repository-owned observations, not causal savings. An UNKNOWN metric "
    "is missing evidence, never a zero, and an insufficient sample is never an "
    "inferred average."
)

UNKNOWN = "UNKNOWN"

PROFILE_CPP_LARGE = "cpp_large_expensive"
PROFILE_TS_JS_MONOREPO = "ts_js_monorepo"
PROFILE_PYTHON_BACKEND_DATA = "python_backend_or_data"
PROFILE_SUBMODULES_LFS_STRICT_CI = "submodules_or_lfs_strict_ci"

# The corpus is heterogeneous by construction: a manifest that cannot cover all
# four profiles cannot qualify the workforce against difficult repositories.
REQUIRED_PROFILES: frozenset[str] = frozenset({
    PROFILE_CPP_LARGE,
    PROFILE_TS_JS_MONOREPO,
    PROFILE_PYTHON_BACKEND_DATA,
    PROFILE_SUBMODULES_LFS_STRICT_CI,
})

# The checked-in corpus is fixed and pinned. Each entry was pinned by reading
# the repository's own remote ref advertisement for a release tag -- read-only
# metadata, which is neither a clone, a build nor an execution.
CORPUS_MANIFEST_STATE = "PINNED"

CORPUS_MANIFEST_STATE_REASON = (
    "materialized: every entry pins an immutable full-length commit object id observed by "
    "reading the repository's own remote ref advertisement for a release tag, and carries the "
    "pin_evidence recording that observation. Reading ref metadata is not a clone, a build or "
    "an execution, so the fixed corpus is materialized without crossing the phase boundary."
)

CORPUS_VERSION = "2026.09.0"

_PIN_OBSERVATION_METHODS: frozenset[str] = frozenset({"git_ls_remote"})

_IMMUTABLE_REF_PREFIX = "refs/tags/"

_ISO_DATE_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$")

# When the pins below were read. The commit ids are immutable, so this records
# when the ref was observed, not a freshness requirement on the corpus.
_PIN_OBSERVED_ON = "2026-09-13"

# What each profile has to demonstrate for the corpus to be difficult in the way
# this qualification cares about. The repository satisfying each profile is
# pinned in FIXED_CORPUS_ENTRIES, below.
PROFILE_REQUIREMENTS: dict[str, dict[str, Any]] = {
    PROFILE_CPP_LARGE: {
        "summary": "Large, expensive-to-build C++ project.",
        "must_demonstrate": [
            "multi-hour or heavily cached native build",
            "translation units large enough to defeat naive whole-file context",
            "compiler/toolchain configuration that must be discovered, not assumed",
        ],
    },
    PROFILE_TS_JS_MONOREPO: {
        "summary": "TypeScript or JavaScript monorepo.",
        "must_demonstrate": [
            "many packages behind one workspace root",
            "cross-package type resolution and build ordering",
            "lint/typecheck gates distinct from the test gate",
        ],
    },
    PROFILE_PYTHON_BACKEND_DATA: {
        "summary": "Python backend or data project.",
        "must_demonstrate": [
            "runtime dependency surface beyond the standard library",
            "database, scheduler or pipeline state in the test path",
            "a test suite long enough that selection matters",
        ],
    },
    PROFILE_SUBMODULES_LFS_STRICT_CI: {
        "summary": "Submodules or Git LFS, under strict CI.",
        "must_demonstrate": [
            "git submodules or LFS objects required before a build can start",
            "CI that fails closed on formatting, licensing or generated-file drift",
            "checkout cost that makes a naive clone-per-attempt untenable",
        ],
    },
}

SUPPORTED_PLATFORMS: frozenset[str] = frozenset({"linux", "macos", "windows"})

# A Linux development host cannot witness a Windows or macOS result, so either
# claim must name its own retrievable, digest-bearing locator.
EVIDENCE_REQUIRED_PLATFORMS: frozenset[str] = frozenset({"macos", "windows"})

OUTCOME_ACCEPTED = "accepted"
OUTCOME_REJECTED = "rejected"
OUTCOME_FAILED = "failed"
OUTCOMES: frozenset[str] = frozenset({OUTCOME_ACCEPTED, OUTCOME_REJECTED, OUTCOME_FAILED})

EVIDENCE_STATES: frozenset[str] = frozenset({"pass", "fail", UNKNOWN})

# Progress signals. Each one is worth recording and none of them is acceptance.
NON_ACCEPTANCE_SIGNALS: frozenset[str] = frozenset({
    "launch_succeeded",
    "process_launched",
    "review_ready",
    "review_submitted",
    "test_invocation",
    "tests_invoked",
    "validation_passed",
    "worker_completed",
    "worker_exit_zero",
})

REVISION_KIND = "git_commit_oid"

_GIT_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_PREFIXED_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ROUTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")

# Named refs move. Rejecting them by name gives a precise reason before the
# stricter "must be a full-length oid" check reports a shape problem.
_FLOATING_REVISIONS: frozenset[str] = frozenset({
    "default", "dev", "develop", "head", "latest", "main", "master",
    "next", "release", "stable", "tip", "trunk",
})

_RETRIEVABLE_SCHEMES: frozenset[str] = frozenset({"artifact", "ci", "https"})

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Owner-local root segments. Windows and macOS filesystems are case-insensitive
# by default, so ``/users/owner`` and ``/Home/owner`` name exactly the same
# owner-local directories as their canonical spellings. Matching a single casing
# would let the other spelling be sealed into a replayable artifact, so the
# segment names below are matched case-insensitively. Case folding only widens
# what is rejected: the lookbehinds and the required separator are unchanged, so
# no previously rejected string is now accepted.
#
# ``library`` and ``volumes`` are the macOS roots the POSIX list above misses.
# A macOS run's own report lands under ``/Users`` most of the time, but
# ``/Volumes/<disk>/...`` is where an external or network-mounted disk appears
# and ``/Library/...`` is where per-machine application state lives. Both are
# owner-local, neither is retrievable from any other host, and a macOS evidence
# reference is exactly the field that would otherwise carry one.
_LOCAL_ROOT_SEGMENTS = r"(?i:home|users|root|var|tmp|opt|private|mnt|srv|library|volumes)"

# Owner-local paths are rejected wherever they appear, so no alternative below
# requires a leading text boundary: free text embeds a path after punctuation a
# boundary class never covered -- ``macos evidence:/Users/owner/report.json``,
# ``rejected,/home/owner/report.json``, ``rejected-/home/owner/report.json``.
#
# The one context that must stay accepted is a URL's own authority and path:
# ``https://ci.example.com/var/log/run-1`` names a directory on a build host, not
# this machine's ``/var``. That context cannot be read off the character to the
# left of the path. A host or a URL path segment ends in the same characters
# prose does, so any lookbehind wide enough to exempt ``.../build-/var/log`` also
# exempts ``rejected-/home/owner/report.json``; separating them means reaching
# back to the ``://`` that opens the authority, and a fixed-width lookbehind
# cannot reach that far.
#
# So a whole URL -- scheme, host-bearing authority and path -- is matched as its
# own alternative, and only a match of the ``local`` group is a violation. Each
# owner-local alternative then carries one narrow left boundary: not preceded by
# an alphanumeric, which keeps a relative path such as ``docs/var/log`` out. A
# ``/`` in front stays a match on purpose, since ``artifact:///home/...`` is the
# authority-less spelling of the same owner-local path, and the authority below
# must hold an alphanumeric, so ``ci://-/home/...`` is no URL context either.
#
# The tilde alternative also covers ``~user``. ``~`` alone means the running
# owner's home, but ``~shrek/report.json`` names the same home directory
# explicitly, and the scheme-relative pattern below already treats a bare
# ``~user`` as owner-local. An optional user name keeps the two consistent.
# It must start with a letter or underscore so an approximation in prose --
# ``retries ~3/attempt``, ``~50/sec`` -- stays free text rather than a path.
# The URL alternative is what keeps ``https://ci.example.com/~runner/...`` -- a
# per-user CI directory and a retrievable locator -- out of the tilde's reach,
# on exactly the same terms as the rooted alternative. ``~shrek/report.json``
# and ``checkout:~/ci/report.json`` carry no URL authority, so both stay
# rejected.
# A URL token ends where the URL ends, not where the line does. Matching the
# path as "everything up to whitespace" made that exemption greedy enough to
# swallow the token behind it: ``https://ci.example.com/run/1,/home/owner/report.json``
# holds no whitespace at all, so the owner-local tail rode out inside the ``url``
# group, where the ``local`` group never looks. Adjacency is the whole trick --
# one separator character in front of the path is all it takes.
#
# So the authority and the path both stop at the characters that cannot carry a
# URL forward: the ones RFC 3986 excludes from a URI outright (whitespace, a
# backslash, ``"``, ``<``, ``>``, ``^``, a backquote, ``{``, ``|``, ``}``), the
# ``?`` and ``#`` that close a path and open a query or fragment rather than
# extending it, and the separators that end a URL inside running prose (``,``,
# ``;``, ``'`` and the bracket pairs). Stopping earlier can only reject more,
# never less: the remainder goes back to the scan as ordinary free text, so
# every owner-local alternative below gets to see it. The punctuation that
# genuinely continues a path -- ``.``, ``-``, ``_``, ``~``, ``%``, ``+`` -- is
# deliberately absent, which is what keeps a published CI path such as
# ``https://ci.example.com/run-1-/var/log/report.json`` one URL token.
_URL_STOP = r"""\s\\"'`^{|}<>?#,;()\[\]"""

_URL_AUTHORITY_AND_PATH = (
    r"[A-Za-z][A-Za-z0-9+.-]*://"                        # scheme
    rf"(?=[^{_URL_STOP}/]*[A-Za-z0-9])[^{_URL_STOP}/]+"  # authority naming a host
    rf"(?:/[^{_URL_STOP}]*)?"                            # path, ending where the URL does
)

_LOCAL_PATH_RE = re.compile(
    rf"(?P<url>{_URL_AUTHORITY_AND_PATH})"
    r"|(?P<local>"
    r"(?<![A-Za-z0-9])~(?:[A-Za-z_][A-Za-z0-9_.-]*)?[/\\]"
    rf"|(?<![A-Za-z0-9])/{_LOCAL_ROOT_SEGMENTS}[/\\]"
    r"|(?<![A-Za-z0-9])[A-Za-z]:[\\/]"
    r"|\\\\[A-Za-z0-9_.-]+\\"
    r")"
)

# The whole-string scan above still cannot decide every spelling a retrievable
# scheme hides, because those are not path shapes on their own: ``ci://C:`` has
# no path after the drive letter and ``artifact://~shrek/...`` is a home
# reference with no separator. This pattern is anchored at the start of a
# locator's scheme-relative remainder instead, so a retrievable scheme cannot
# launder an owner-local path in any spelling or casing.
_SCHEME_RELATIVE_LOCAL_PATH_RE = re.compile(
    r"^(?:~"
    rf"|/{_LOCAL_ROOT_SEGMENTS}(?:[/\\]|$)"
    r"|[A-Za-z]:(?:[\\/]|$)"
    r"|\\)"
)

# The anchor above only sees a path that starts immediately after ``://``. A single
# punctuation character in front of it -- ``artifact://./Users/…``, ``ci://-/home/…``,
# ``artifact://_/var/…`` -- moves the path past the anchor while still satisfying the
# "authority is nonempty" check, and ``.``, ``-`` and ``_`` all sit inside the whole-string
# scan's negative lookbehind too. A host is not punctuation: requiring one alphanumeric
# character separates a real authority from a character standing in for the empty one.
_AUTHORITY_HOST_RE = re.compile(r"[A-Za-z0-9]")

_SECRET_RE = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:authorization\s*[:=]\s*\S)"
    r"|(?i:(?:api[_-]?key|secret|passwd|password|access[_-]?token)\s*[:=]\s*\S)"
)

_FORBIDDEN_KEYS: frozenset[str] = frozenset({
    "access_token", "api_key", "apikey", "authorization", "credential",
    "credentials", "passwd", "password", "private_key", "refresh_token",
    "secret", "session_key", "ssh_key", "token",
})


class ExternalQualificationError(ValueError):
    """Base error for every external qualification contract violation."""


class InvalidCorpusError(ExternalQualificationError):
    """Raised when the corpus manifest fails structural validation."""


class InvalidRunArtifactError(ExternalQualificationError):
    """Raised when a qualification run artifact fails structural validation."""


_ErrorType = type[ExternalQualificationError]


def canonical_json(value: Any) -> str:
    """Serialize ``value`` so equal content always produces equal bytes."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_digest(value: Any) -> str:
    """Return the ``sha256:`` digest of the canonical serialization of ``value``."""
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _contains_owner_local_path(text: str) -> bool:
    """Report an owner-local path sitting anywhere outside a URL authority and path.

    ``_LOCAL_PATH_RE`` matches a whole URL as its own alternative so a remote path
    segment is never read as a local one. That makes a bare ``search`` ambiguous:
    only a match of the ``local`` group is a violation.
    """
    return any(match.group("local") for match in _LOCAL_PATH_RE.finditer(text))


def _reject_forbidden_text(text: str, *, field: str, error: _ErrorType) -> None:
    if _CONTROL_CHARS_RE.search(text):
        raise error(f"{field}: control characters are not allowed")
    if _SECRET_RE.search(text):
        raise error(f"{field}: credential-shaped content rejected")
    if "file://" in text.lower():
        raise error(f"{field}: local file:// reference rejected")
    if _contains_owner_local_path(text):
        raise error(f"{field}: owner-local filesystem path rejected")


def _scan(value: Any, *, field: str, error: _ErrorType) -> None:
    """Reject secrets, owner-local paths and unserializable values anywhere in ``value``."""
    if isinstance(value, str):
        _reject_forbidden_text(value, field=field, error=error)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise error(f"{field}: mapping keys must be strings")
            if key.strip().lower().replace("-", "_") in _FORBIDDEN_KEYS:
                raise error(f"{field}.{key}: credential-bearing field rejected")
            _scan(item, field=f"{field}.{key}", error=error)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan(item, field=f"{field}[{index}]", error=error)
    elif isinstance(value, float) and not math.isfinite(value):
        raise error(f"{field}: non-finite number is not canonically serializable")
    elif not isinstance(value, (bool, int, float)) and value is not None:
        raise error(f"{field}: unsupported value type {type(value).__name__}")


def _require_str(
    container: dict[str, Any],
    key: str,
    *,
    field: str,
    error: _ErrorType,
    pattern: re.Pattern[str] | None = None,
) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise error(f"{field}.{key}: non-empty string required")
    value = value.strip()
    if pattern is not None and not pattern.match(value):
        raise error(f"{field}.{key}: malformed value {value!r}")
    return value


def _require_index(container: dict[str, Any], key: str, *, field: str, error: _ErrorType) -> int:
    value = container.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise error(f"{field}.{key}: non-negative integer required")
    return value


def _validate_repository_url(url: str, *, field: str, error: _ErrorType) -> str:
    """Accept only a public, credential-free https repository URL."""
    if not url.startswith("https://"):
        raise error(f"{field}: public https:// repository URL required, got {url!r}")
    host, separator, path = url[len("https://"):].partition("/")
    if "@" in host:
        raise error(f"{field}: embedded URL credentials rejected")
    if not host or "." not in host or not separator or not path:
        raise error(f"{field}: malformed public repository URL {url!r}")
    if "?" in url or "#" in url:
        raise error(f"{field}: query and fragment components rejected")
    return url


def _repository_identity(url: str) -> str:
    """Collapse cosmetic URL spellings so two spellings of one repository collide.

    Lowercasing comes before the suffix strip: ``.GIT`` is the same cosmetic
    suffix as ``.git``, and stripping first would let it survive as part of the
    identity and slip past duplicate detection.
    """
    return url.rstrip("/").lower().removesuffix(".git")


def _validate_immutable_revision(revision: str, *, field: str, error: _ErrorType) -> str:
    """Accept only a full-length commit object id, never a branch, tag or short sha."""
    lowered = revision.lower()
    if lowered in _FLOATING_REVISIONS or lowered.startswith(("refs/", "origin/")):
        raise error(f"{field}: floating revision or default branch rejected: {revision!r}")
    if not _GIT_OID_RE.match(lowered):
        raise error(
            f"{field}: immutable full-length commit object id required, got {revision!r}"
        )
    return lowered


def _validate_pin_evidence(
    raw: Any, *, revision: str, repository_url: str, field: str, error: _ErrorType
) -> dict[str, Any]:
    """Require the primary-source observation that produced one revision pin.

    A revision nobody observed is a fabricated pin, so an entry has to say which
    immutable ref it read, what that ref advertised, when it was read, and under
    which publicly retrievable locator the pinned commit can be re-read. For an
    annotated tag ``observed_ref_object_id`` is the tag object and ``revision``
    is the commit it peels to; for a lightweight tag the two coincide.
    """
    if not isinstance(raw, dict):
        raise error(f"{field}: pin evidence mapping required")
    method = _require_str(raw, "observation_method", field=field, error=error)
    if method not in _PIN_OBSERVATION_METHODS:
        raise error(
            f"{field}.observation_method: one of {sorted(_PIN_OBSERVATION_METHODS)} required, "
            f"got {method!r}"
        )
    observed_ref = _require_str(raw, "observed_ref", field=field, error=error)
    if not observed_ref.startswith(_IMMUTABLE_REF_PREFIX) or observed_ref == _IMMUTABLE_REF_PREFIX:
        raise error(
            f"{field}.observed_ref: an immutable {_IMMUTABLE_REF_PREFIX}… ref required, "
            f"got {observed_ref!r}"
        )
    # ``_GIT_OID_RE`` only matches lowercase hex, so the fold has to come before the
    # match, not after it. ``_validate_immutable_revision`` already applies exactly that
    # ordering -- normalize, validate, return the canonical lowercase spelling -- to the
    # pinned revision, so reusing it here gives every field carrying a git oid one rule
    # instead of two. A tag object id is an oid like any other: a named ref in this field
    # is as unpinned as one in ``repository_revision``.
    ref_object_id = _validate_immutable_revision(
        _require_str(raw, "observed_ref_object_id", field=field, error=error),
        field=f"{field}.observed_ref_object_id",
        error=error,
    )
    observed_on = _require_str(raw, "observed_on", field=field, error=error, pattern=_ISO_DATE_RE)
    locator = _validate_repository_url(
        _require_str(raw, "locator", field=field, error=error),
        field=f"{field}.locator",
        error=error,
    )
    # ``revision`` arrives already folded by ``_validate_immutable_revision``, so an entry
    # that spells the same commit in uppercase would fail this check against its own matching
    # locator. A git oid is case-insensitive, so compare both operands folded and then seal
    # the canonical lowercase spelling: one commit must not serialize to two digests.
    pinned = revision.lower()
    if not locator.lower().endswith("/" + pinned):
        raise error(
            f"{field}.locator: must resolve the pinned revision {revision!r}, got {locator!r}"
        )
    locator = locator[: len(locator) - len(pinned)] + pinned
    # Containment has to read the repository the same way duplicate detection does.
    # ``_repository_identity`` already rules that a trailing slash, a ``.git`` suffix and
    # host case are cosmetic, so comparing raw ``repository_url`` bytes here contradicted
    # the module's own identity: a ``…/llvm-project.git`` pin and its canonical
    # ``…/llvm-project/commit/<oid>`` locator name one repository and must not be rejected.
    repository = _repository_identity(repository_url)
    contained = _repository_identity(locator)
    # The suffix is cosmetic wherever the repository segment ends, not only at the end of
    # the string, so a locator that keeps ``.git`` in its own repository segment collapses
    # to the same identity instead of failing containment from the opposite direction.
    if contained.startswith(repository + ".git/"):
        contained = repository + contained[len(repository) + len(".git") :]
    if not contained.startswith(repository + "/"):
        raise error(
            f"{field}.locator: must point into {repository_url!r}, got {locator!r}"
        )
    return {
        "locator": locator,
        "observation_method": method,
        "observed_on": observed_on,
        "observed_ref": observed_ref,
        "observed_ref_object_id": ref_object_id,
    }


def _validate_evidence_reference(
    reference: Any, *, field: str, error: _ErrorType
) -> dict[str, Any]:
    """Require a retrievable locator carrying its own content digest."""
    if not isinstance(reference, dict):
        raise error(f"{field}: evidence reference must be a mapping")
    locator = _require_str(reference, "locator", field=field, error=error)
    scheme, separator, remainder = locator.partition("://")
    if not separator or scheme.lower() not in _RETRIEVABLE_SCHEMES:
        schemes = "|".join(sorted(_RETRIEVABLE_SCHEMES))
        raise error(f"{field}.locator: retrievable {schemes}:// locator required, got {locator!r}")
    # A retrievable scheme in front of a filesystem path does not make the path
    # retrievable, and the whole-string scan cannot see it: the path starts
    # immediately after ``://`` rather than at a text boundary. Validate the
    # authority and path directly so ``ci://C:/Users/...`` and
    # ``artifact:///home/...`` are rejected as the owner-local paths they are.
    if _SCHEME_RELATIVE_LOCAL_PATH_RE.match(remainder) or "\\" in remainder:
        raise error(
            f"{field}.locator: owner-local filesystem path behind a retrievable "
            f"scheme rejected: {locator!r}"
        )
    authority = remainder.partition("/")[0]
    if not authority:
        raise error(
            f"{field}.locator: authority-less locator is an owner-local filesystem path, "
            f"not a retrievable reference: {locator!r}"
        )
    if "@" in authority:
        raise error(f"{field}.locator: malformed or credential-bearing locator {locator!r}")
    # ``.``, ``-`` and ``_`` are nonempty but name no host, so ``artifact://./Users/owner``,
    # ``ci://-/home/owner`` and ``artifact://_/var/tmp`` are the authority-less spellings
    # above with one character of punctuation inserted. That character is enough to move the
    # path off the scheme-relative anchor and behind the whole-string scan's lookbehind at
    # once, so re-anchor the owner-local check on the path that follows the authority
    # whenever the authority names no host. A real host always carries an alphanumeric, so
    # nothing retrievable reaches this branch and ``ci://run/1/…`` stays accepted.
    if not _AUTHORITY_HOST_RE.search(authority):
        if _SCHEME_RELATIVE_LOCAL_PATH_RE.match(remainder[len(authority):]):
            raise error(
                f"{field}.locator: owner-local filesystem path behind a retrievable "
                f"scheme rejected: {locator!r}"
            )
        raise error(
            f"{field}.locator: authority {authority!r} names no retrievable host: {locator!r}"
        )
    digest = _require_str(reference, "sha256", field=field, error=error, pattern=_SHA256_HEX_RE)
    media_type = _require_str(reference, "media_type", field=field, error=error)
    byte_count = reference.get("byte_count", UNKNOWN)
    if byte_count != UNKNOWN:
        byte_count = _require_index(reference, "byte_count", field=field, error=error)
    return {
        "locator": locator,
        "sha256": digest,
        "media_type": media_type,
        "byte_count": byte_count,
    }


def _validate_evidence_block(raw: Any, *, field: str, error: _ErrorType) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise error(f"{field}: mapping required")
    state = _require_str(raw, "state", field=field, error=error)
    if state not in EVIDENCE_STATES:
        raise error(f"{field}.state: one of {sorted(EVIDENCE_STATES)} required, got {state!r}")
    raw_references = raw.get("evidence", [])
    if not isinstance(raw_references, list):
        raise error(f"{field}.evidence: list required")
    references = [
        _validate_evidence_reference(reference, field=f"{field}.evidence[{index}]", error=error)
        for index, reference in enumerate(raw_references)
    ]
    if state != UNKNOWN and not references:
        raise error(f"{field}: a decided state requires at least one retrievable reference")
    references.sort(key=lambda reference: (reference["locator"], reference["sha256"]))
    return {"state": state, "evidence": references}


def _validate_usage(raw: Any, *, field: str, error: _ErrorType) -> dict[str, Any]:
    """Normalize observed usage, preserving UNKNOWN rather than inventing a zero."""
    if not isinstance(raw, dict):
        raise error(f"{field}: usage must be a mapping")
    usage: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = raw.get(key, UNKNOWN)
        if value == UNKNOWN:
            usage[key] = UNKNOWN
            continue
        usage[key] = _require_index(raw, key, field=field, error=error)
    cost = raw.get("cost_usd", UNKNOWN)
    if cost == UNKNOWN:
        usage["cost_usd"] = UNKNOWN
    elif isinstance(cost, bool) or not isinstance(cost, (int, float)):
        raise error(f"{field}.cost_usd: non-negative finite number or UNKNOWN required")
    elif not math.isfinite(float(cost)) or float(cost) < 0.0:
        raise error(f"{field}.cost_usd: non-negative finite number or UNKNOWN required")
    else:
        usage["cost_usd"] = round(float(cost), 6)
    usage["cost_known"] = usage["cost_usd"] != UNKNOWN
    usage["tokens_known"] = all(
        usage[key] != UNKNOWN for key in ("input_tokens", "output_tokens", "total_tokens")
    )
    for key in ("cost_known", "tokens_known"):
        declared = raw.get(key)
        if declared is not None and bool(declared) is not usage[key]:
            raise error(f"{field}.{key}: declared flag contradicts the observed evidence")
    return usage


# The canonical builder emits ``attempt_artifact_manifest_id`` bare while
# ``receipt_id`` and ``repository_revision`` carry the ``sha256:`` prefix; the
# canonical validator accepts either spelling for all three.
_SHA256_ANY_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")

_RECEIPT_DIGEST_PATTERNS: dict[str, re.Pattern[str]] = {
    "receipt_id": _SHA256_PREFIXED_RE,
    "attempt_artifact_manifest_id": _SHA256_ANY_RE,
    "repository_revision": _SHA256_PREFIXED_RE,
}


def _validate_accepted_outcome_receipt(
    raw: Any, *, task_id: str, request_id: str, field: str, error: _ErrorType
) -> dict[str, Any]:
    """Require the canonical ``aiworkhub.accepted_outcome_receipt.v1`` identity.

    This is not a second acceptance authority. It admits exactly the field set
    ``task_engine._validate_accepted_outcome_receipt`` admits, recomputes
    ``repository_revision`` and ``receipt_id`` with that module's own canonical
    hash, and binds the receipt to this attempt -- so an artifact can never
    carry a receipt the canonical authority would reject. Everything that needs
    the repository on disk (sealed terminal evidence, canonical file hashes,
    claim epoch) stays with the canonical authority itself; bind one with
    :func:`canonical_acceptance_authority`.
    """
    if not isinstance(raw, dict):
        raise error(f"{field}: accepted outcome receipt must be a mapping")
    if set(raw) != ACCEPTED_OUTCOME_RECEIPT_FIELDS:
        missing = sorted(ACCEPTED_OUTCOME_RECEIPT_FIELDS - set(raw))
        unexpected = sorted(set(raw) - ACCEPTED_OUTCOME_RECEIPT_FIELDS)
        raise error(
            f"{field}: exact {ACCEPTED_OUTCOME_RECEIPT_SCHEMA} field set required "
            f"(missing={missing}, unexpected={unexpected})"
        )
    if raw.get("schema_id") != ACCEPTED_OUTCOME_RECEIPT_SCHEMA:
        raise error(f"{field}.schema_id: must be {ACCEPTED_OUTCOME_RECEIPT_SCHEMA!r}")
    if _require_str(raw, "task_id", field=field, error=error) != task_id:
        raise error(f"{field}.task_id: receipt identity does not bind this task")
    if _require_str(raw, "request_id", field=field, error=error) != request_id:
        raise error(f"{field}.request_id: receipt identity does not bind this attempt")
    claim_epoch = raw.get("claim_epoch")
    if not isinstance(claim_epoch, int) or isinstance(claim_epoch, bool) or claim_epoch < 0:
        raise error(f"{field}.claim_epoch: non-negative integer claim epoch required")
    # ``base_oid`` is the one git oid in this module that is deliberately *not* folded.
    # It is a preimage of a digest the canonical authority already sealed, so any
    # normalization here would recompute a ``repository_revision`` the canonical builder
    # never emitted and reject the receipt it was supposed to admit. The digests below
    # are compared byte-exact for the same reason: they are recomputed, not re-spelled.
    base_oid = _require_str(raw, "base_oid", field=field, error=error)

    # The admission rule here is the canonical one and nothing beyond it, empty
    # promotion included. A readonly research or quality-review acceptance
    # promotes no bytes at all: the canonical authority seals
    # ``promoted_paths: []`` beside ``changed_path_hashes: {}`` for it, checks
    # only that the list holds sorted, deduplicated strings, and admits the
    # pair. Requiring a promotion -- or a non-blank spelling -- would make this a
    # second, stricter acceptance authority that refuses receipts the only one
    # grants, which is exactly what the docstring above says this is not.
    promoted = raw.get("promoted_paths")
    if (
        not isinstance(promoted, list)
        or any(not isinstance(path, str) for path in promoted)
        or promoted != sorted(set(promoted))
    ):
        raise error(
            f"{field}.promoted_paths: sorted, deduplicated relative path list required"
        )
    hashes = raw.get("changed_path_hashes")
    if not isinstance(hashes, dict) or sorted(hashes) != promoted:
        raise error(
            f"{field}.changed_path_hashes: exactly one canonical hash per promoted path required"
        )
    for path, digest in hashes.items():
        if digest is not None and not (
            isinstance(digest, str) and _SHA256_HEX_RE.match(digest)
        ):
            raise error(f"{field}.changed_path_hashes.{path}: sha256 hex digest or null required")
    for key, pattern in _RECEIPT_DIGEST_PATTERNS.items():
        _require_str(raw, key, field=field, error=error, pattern=pattern)

    # Both digests are recomputed with the canonical hash, so a hand-assembled
    # receipt cannot claim an identity its own contents do not produce.
    revision = "sha256:" + _canonical_json_hash(
        {"base_oid": base_oid, "changed_path_hashes": hashes}
    )
    if raw["repository_revision"] != revision:
        raise error(
            f"{field}.repository_revision: does not match the base oid and canonical "
            "changed-path hashes this receipt carries"
        )
    unsigned = {key: value for key, value in raw.items() if key != "receipt_id"}
    if raw["receipt_id"] != "sha256:" + _canonical_json_hash(unsigned):
        raise error(f"{field}.receipt_id: recomputed canonical receipt digest does not match")
    return dict(sorted(raw.items()))


def bind_acceptance_authority(
    authority: AcceptanceAuthority, *, task_id: str, request_id: str
) -> AcceptanceAuthority:
    """Declare the single attempt ``authority`` is entitled to speak for.

    An acceptance authority is bound to one ``(task_id, request_id)`` when it is
    built, but a bare callable does not carry that binding, so a caller holding
    one cannot tell "this receipt is bad" from "this receipt is not mine". The
    declared binding makes that distinction checkable, which is what lets
    :func:`validate_run_artifact` record an out-of-scope row instead of failing
    the batch it arrived in.

    The wrapper is deliberately thin: it delegates every verdict to
    ``authority``. Declaring a binding narrows what the authority is *asked*, and
    never widens or weakens what it grants.
    """
    for name, value in (("task_id", task_id), ("request_id", request_id)):
        if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
            raise ValueError(f"bind_acceptance_authority: {name} must be an identifier")

    def _bound(receipt: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        return authority(receipt)

    _bound.acceptance_binding = (task_id, request_id)  # type: ignore[attr-defined]
    return _bound


def acceptance_authority_binding(
    authority: AcceptanceAuthority | None,
) -> tuple[str, str] | None:
    """Return the ``(task_id, request_id)`` ``authority`` speaks for, if declared.

    ``None`` means the authority made no claim about its scope, so it is asked
    about every row -- an undeclared binding must not silently excuse a row from
    authentication.
    """
    binding = getattr(authority, "acceptance_binding", None)
    if (
        isinstance(binding, tuple)
        and len(binding) == 2
        and all(isinstance(part, str) for part in binding)
    ):
        return binding
    return None


def canonical_acceptance_authority(
    repo: Any, card: dict[str, Any], *, task_id: str, request_id: str
) -> AcceptanceAuthority:
    """Bind the canonical task-engine receipt validator to one repository and card.

    The returned callable *is* ``task_engine._validate_accepted_outcome_receipt``
    with its repository-bound arguments supplied -- not a reimplementation of its
    rules -- so this module cannot drift into a weaker second acceptance
    authority. It returns the canonical ``(receipt, reason)`` pair, where a
    non-empty reason is the canonical refusal code.

    The result carries its ``(task_id, request_id)`` binding, so a row from
    another attempt is recognized as out of scope rather than mistaken for a
    receipt this authority refused.
    """

    def _authority(receipt: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        return _canonical_receipt_authority(repo, card, task_id, request_id, receipt)

    return bind_acceptance_authority(_authority, task_id=task_id, request_id=request_id)


def _seal(body: dict[str, Any], *, key: str, provided: Any, error: _ErrorType) -> dict[str, Any]:
    """Stamp the canonical digest of ``body`` and reject a contradicting declared id."""
    digest = canonical_digest(body)
    if provided is not None and provided != digest:
        raise error(f"{key}: declared identity does not match the canonical digest")
    body[key] = digest
    return body


def validate_corpus_manifest(manifest: Any) -> dict[str, Any]:
    """Validate and normalize the fixed heterogeneous external corpus manifest.

    Returns a deterministically ordered manifest sealed with
    ``corpus_manifest_id``. Entry order in the input never changes the digest.
    """
    error: _ErrorType = InvalidCorpusError
    if not isinstance(manifest, dict):
        raise error("corpus manifest must be a mapping")
    _scan(manifest, field="corpus_manifest", error=error)
    schema_id = _require_str(manifest, "schema_id", field="corpus_manifest", error=error)
    if schema_id != CORPUS_MANIFEST_SCHEMA:
        raise error(f"corpus_manifest.schema_id must be {CORPUS_MANIFEST_SCHEMA!r}")
    corpus_version = _require_str(
        manifest, "corpus_version", field="corpus_manifest", error=error, pattern=_IDENTIFIER_RE
    )
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise error("corpus_manifest.entries: at least one entry required")

    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_repositories: set[str] = set()
    for index, raw in enumerate(raw_entries):
        field = f"corpus_manifest.entries[{index}]"
        if not isinstance(raw, dict):
            raise error(f"{field}: entry must be a mapping")
        corpus_id = _require_str(raw, "corpus_id", field=field, error=error, pattern=_IDENTIFIER_RE)
        if corpus_id in seen_ids:
            raise error(f"{field}.corpus_id: duplicate corpus identity {corpus_id!r}")
        seen_ids.add(corpus_id)
        profile = _require_str(raw, "profile", field=field, error=error)
        if profile not in REQUIRED_PROFILES:
            raise error(f"{field}.profile: unknown profile {profile!r}")
        repository_url = _validate_repository_url(
            _require_str(raw, "repository_url", field=field, error=error),
            field=f"{field}.repository_url",
            error=error,
        )
        identity = _repository_identity(repository_url)
        if identity in seen_repositories:
            raise error(f"{field}.repository_url: duplicate corpus identity {repository_url!r}")
        seen_repositories.add(identity)
        revision_kind = _require_str(raw, "revision_kind", field=field, error=error)
        if revision_kind != REVISION_KIND:
            raise error(f"{field}.revision_kind: must be {REVISION_KIND!r}")
        revision = _validate_immutable_revision(
            _require_str(raw, "repository_revision", field=field, error=error),
            field=f"{field}.repository_revision",
            error=error,
        )
        entries.append({
            "corpus_id": corpus_id,
            "profile": profile,
            "repository_url": repository_url,
            "repository_revision": revision,
            "revision_kind": revision_kind,
            "rationale": _require_str(raw, "rationale", field=field, error=error),
            "pin_evidence": _validate_pin_evidence(
                raw.get("pin_evidence"),
                revision=revision,
                repository_url=repository_url,
                field=f"{field}.pin_evidence",
                error=error,
            ),
        })

    missing = sorted(REQUIRED_PROFILES - {entry["profile"] for entry in entries})
    if missing:
        raise error(f"corpus_manifest.entries: required profiles not covered: {missing}")

    entries.sort(key=lambda entry: entry["corpus_id"])
    body: dict[str, Any] = {
        "schema_id": CORPUS_MANIFEST_SCHEMA,
        "corpus_version": corpus_version,
        "phase_boundary": PHASE_BOUNDARY,
        "required_profiles": sorted(REQUIRED_PROFILES),
        "entries": entries,
    }
    return _seal(
        body,
        key="corpus_manifest_id",
        provided=manifest.get("corpus_manifest_id"),
        error=error,
    )


def corpus_specification() -> dict[str, Any]:
    """Return the per-profile criteria beside the pinned entry that satisfies it.

    This is the criteria view of :func:`fixed_corpus_manifest`, not a second
    corpus: every repository URL, revision and pin observation is read back out
    of the checked-in manifest, so the two cannot drift apart. It carries its
    own ``schema_id`` and is deliberately not accepted by
    :func:`validate_corpus_manifest`, which validates manifests only.
    """
    manifest = fixed_corpus_manifest()
    by_profile = {entry["profile"]: entry for entry in manifest["entries"]}
    body: dict[str, Any] = {
        "schema_id": CORPUS_SPECIFICATION_SCHEMA,
        "phase_boundary": PHASE_BOUNDARY,
        "state": CORPUS_MANIFEST_STATE,
        "reason": CORPUS_MANIFEST_STATE_REASON,
        "corpus_manifest_id": manifest["corpus_manifest_id"],
        "corpus_version": manifest["corpus_version"],
        "required_profiles": sorted(REQUIRED_PROFILES),
        "profiles": {
            profile: {
                "summary": requirement["summary"],
                "must_demonstrate": list(requirement["must_demonstrate"]),
                "corpus_id": by_profile[profile]["corpus_id"],
                "repository_url": by_profile[profile]["repository_url"],
                "repository_revision": by_profile[profile]["repository_revision"],
                "pin_evidence": dict(by_profile[profile]["pin_evidence"]),
            }
            for profile, requirement in sorted(PROFILE_REQUIREMENTS.items())
        },
    }
    return _seal(body, key="corpus_specification_id", provided=None, error=InvalidCorpusError)


def _validate_platforms(
    artifact: dict[str, Any], *, error: _ErrorType
) -> tuple[list[str], dict[str, Any]]:
    raw_claims = artifact.get("platform_claims")
    if not isinstance(raw_claims, list) or not raw_claims:
        raise error("run_artifact.platform_claims: at least one platform claim required")
    claims: list[str] = []
    for index, claim in enumerate(raw_claims):
        field = f"run_artifact.platform_claims[{index}]"
        if not isinstance(claim, str) or claim not in SUPPORTED_PLATFORMS:
            raise error(f"{field}: one of {sorted(SUPPORTED_PLATFORMS)} required")
        if claim in claims:
            raise error(f"{field}: duplicate platform claim {claim!r}")
        claims.append(claim)
    claims.sort()

    raw_evidence = artifact.get("platform_evidence", {})
    if not isinstance(raw_evidence, dict):
        raise error("run_artifact.platform_evidence: mapping required")
    evidence: dict[str, Any] = {}
    for platform, reference in raw_evidence.items():
        field = f"run_artifact.platform_evidence.{platform}"
        if platform not in SUPPORTED_PLATFORMS:
            raise error(f"{field}: unknown platform")
        if platform not in claims:
            raise error(f"{field}: evidence for a platform this artifact does not claim")
        evidence[platform] = _validate_evidence_reference(reference, field=field, error=error)

    unwitnessed = sorted((set(claims) & EVIDENCE_REQUIRED_PLATFORMS) - set(evidence))
    if unwitnessed:
        raise error(
            "run_artifact.platform_evidence: "
            f"{unwitnessed} claimed without a retrievable evidence reference"
        )
    return claims, dict(sorted(evidence.items()))


def validate_run_artifact(
    artifact: Any,
    *,
    corpus_manifest: Any | None = None,
    acceptance_authority: AcceptanceAuthority | None = None,
) -> dict[str, Any]:
    """Validate and normalize one replayable qualification run artifact.

    Accepted, rejected and failed outcomes share this single schema. The
    returned artifact is sealed with ``run_artifact_id`` and carries a derived
    ``success``.

    ``success`` is never a property of the artifact's own contents.
    ``acceptance_authority`` is the repository's real acceptance authority --
    build one with :func:`canonical_acceptance_authority`, which binds
    ``task_engine._validate_accepted_outcome_receipt`` to a repository and card
    -- and only that bound authority, having authenticated the receipt against
    sealed terminal evidence and the on-disk canonical hashes, can make an
    accepted row successful.

    Without it the row fails closed: it is still validated and recorded in full,
    with ``canonical_authority_state`` ``UNKNOWN``, but ``success`` is ``False``
    however well-formed a ``receipt_authentication`` block the caller supplies.
    A hand-assembled ``{"state": "pass", ...}`` therefore cannot project an
    acceptance the canonical authority never granted.

    A bound authority speaks for one attempt only. Given a row from a different
    ``(task_id, request_id)`` it is not consulted at all, and the row fails
    closed the same way, with the reason
    ``acceptance_authority_bound_to_another_attempt``. Its refusal there would
    be about the question rather than the row, so it is not an error: a batch
    spanning several attempts must stay summarizable. Within its own scope the
    authority's refusal *is* about the row, and that is still raised.
    """
    error: _ErrorType = InvalidRunArtifactError
    if not isinstance(artifact, dict):
        raise error("run artifact must be a mapping")
    _scan(artifact, field="run_artifact", error=error)
    schema_id = _require_str(artifact, "schema_id", field="run_artifact", error=error)
    if schema_id != RUN_ARTIFACT_SCHEMA:
        raise error(f"run_artifact.schema_id must be {RUN_ARTIFACT_SCHEMA!r}")

    field = "run_artifact"
    corpus_manifest_id = _require_str(
        artifact, "corpus_manifest_id", field=field, error=error, pattern=_SHA256_PREFIXED_RE
    )
    corpus_version = _require_str(
        artifact, "corpus_version", field=field, error=error, pattern=_IDENTIFIER_RE
    )
    corpus_id = _require_str(
        artifact, "corpus_id", field=field, error=error, pattern=_IDENTIFIER_RE
    )
    profile = _require_str(artifact, "profile", field=field, error=error)
    if profile not in REQUIRED_PROFILES:
        raise error(f"{field}.profile: unknown profile {profile!r}")
    repository_url = _validate_repository_url(
        _require_str(artifact, "repository_url", field=field, error=error),
        field=f"{field}.repository_url",
        error=error,
    )
    repository_revision = _validate_immutable_revision(
        _require_str(artifact, "repository_revision", field=field, error=error),
        field=f"{field}.repository_revision",
        error=error,
    )
    aiworkhub_revision = _validate_immutable_revision(
        _require_str(artifact, "aiworkhub_revision", field=field, error=error),
        field=f"{field}.aiworkhub_revision",
        error=error,
    )
    route = _require_str(artifact, "route", field=field, error=error, pattern=_ROUTE_RE)
    model = _require_str(artifact, "model", field=field, error=error, pattern=_ROUTE_RE)
    # The canonical receipt binds (task_id, request_id); the artifact must carry
    # both so the binding can be checked rather than assumed.
    task_id = _require_str(artifact, "task_id", field=field, error=error, pattern=_IDENTIFIER_RE)
    request_id = _require_str(
        artifact, "request_id", field=field, error=error, pattern=_IDENTIFIER_RE
    )
    attempt_id = _require_str(
        artifact, "attempt_id", field=field, error=error, pattern=_IDENTIFIER_RE
    )
    retries = _require_index(artifact, "retries", field=field, error=error)
    validation = _validate_evidence_block(
        artifact.get("validation"), field=f"{field}.validation", error=error
    )
    review = _validate_evidence_block(artifact.get("review"), field=f"{field}.review", error=error)
    usage = _validate_usage(artifact.get("usage", {}), field=f"{field}.usage", error=error)
    platform_claims, platform_evidence = _validate_platforms(artifact, error=error)

    outcome = _require_str(artifact, "outcome", field=field, error=error)
    if outcome not in OUTCOMES:
        raise error(f"{field}.outcome: one of {sorted(OUTCOMES)} required, got {outcome!r}")
    outcome_reason = _require_str(artifact, "outcome_reason", field=field, error=error)

    raw_receipt = artifact.get("accepted_outcome_receipt")
    raw_authentication = artifact.get("receipt_authentication")
    receipt: dict[str, Any] | None = None
    receipt_authentication: dict[str, Any] | None = None
    # Only a bound canonical authority that actually ran may set this to
    # ``pass``. No caller-supplied value can reach it, which is exactly what
    # stops a well-shaped but hand-assembled authentication block from
    # projecting an acceptance nobody granted.
    authority_state = "not_applicable"
    authority_reason = "outcome_is_not_an_acceptance"
    if outcome == OUTCOME_ACCEPTED:
        if outcome_reason.lower() in NON_ACCEPTANCE_SIGNALS:
            raise error(
                f"{field}.outcome_reason: {outcome_reason!r} is a progress signal and "
                "cannot be projected as an accepted outcome"
            )
        if raw_receipt is None:
            raise error(
                f"{field}.accepted_outcome_receipt: manager acceptance identity required; "
                "launch, worker completion and test invocation are not acceptance"
            )
        receipt = _validate_accepted_outcome_receipt(
            raw_receipt,
            task_id=task_id,
            request_id=request_id,
            field=f"{field}.accepted_outcome_receipt",
            error=error,
        )
        receipt_authentication = _validate_evidence_block(
            raw_authentication, field=f"{field}.receipt_authentication", error=error
        )
        for name, block in (("validation", validation), ("review", review)):
            if block["state"] != "pass":
                raise error(
                    f"{field}.{name}: an accepted outcome requires passing {name} evidence"
                )
        if acceptance_authority is None:
            # Fail closed. The row is still recorded in full -- an acceptance the
            # authority could not be asked about is evidence, not a gap -- but it
            # is recorded as unauthenticated, and unauthenticated is not success.
            authority_state = UNKNOWN
            authority_reason = "acceptance_authority_not_consulted"
        elif acceptance_authority_binding(acceptance_authority) not in (
            None,
            (task_id, request_id),
        ):
            # Out of scope, not refused. A bound authority speaks for exactly the
            # attempt it was bound to, so asking it about a neighbouring attempt
            # returns its identity refusal -- an answer about the question, not a
            # verdict on this row. Raising on it would let one foreign row abort
            # every batch containing it, and a summary spanning several attempts
            # cannot hold more than one attempt's authority. The row is recorded
            # unauthenticated instead, exactly as it would be with no authority.
            authority_state = UNKNOWN
            authority_reason = "acceptance_authority_bound_to_another_attempt"
        else:
            authenticated, reason = acceptance_authority(receipt)
            if authenticated is None or reason:
                raise error(
                    f"{field}.accepted_outcome_receipt: the canonical acceptance authority "
                    f"rejected this receipt: {reason or 'unauthenticated'}"
                )
            if receipt_authentication["state"] != "pass":
                raise error(
                    f"{field}.receipt_authentication: declared state "
                    f"{receipt_authentication['state']!r} contradicts the canonical "
                    "acceptance authority"
                )
            authority_state = "pass"
            authority_reason = "canonical_authority_authenticated_receipt"
    else:
        if raw_receipt is not None:
            raise error(
                f"{field}.accepted_outcome_receipt: only an accepted outcome may carry a receipt"
            )
        if raw_authentication is not None:
            raise error(
                f"{field}.receipt_authentication: only an accepted outcome may carry "
                "receipt authentication"
            )

    # Success is the canonical authority's verdict, never the artifact's own
    # claim about it. A receipt no authority has authenticated is a recorded
    # claim: the row keeps ``outcome: accepted`` and reports ``success: false``.
    success = (
        outcome == OUTCOME_ACCEPTED
        and receipt is not None
        and authority_state == "pass"
        and receipt_authentication is not None
        and receipt_authentication["state"] == "pass"
    )
    declared_authority_state = artifact.get("canonical_authority_state")
    if declared_authority_state is not None and declared_authority_state != authority_state:
        raise error(
            f"{field}.canonical_authority_state: declared {declared_authority_state!r} "
            f"contradicts the authority actually consulted ({authority_state!r})"
        )
    # Sealed beside the state, so a replay cannot keep ``UNKNOWN`` while quietly
    # relabelling *why* the authority stayed silent.
    declared_authority_reason = artifact.get("canonical_authority_reason")
    if declared_authority_reason is not None and declared_authority_reason != authority_reason:
        raise error(
            f"{field}.canonical_authority_reason: declared {declared_authority_reason!r} "
            f"contradicts the authority actually consulted ({authority_reason!r})"
        )
    declared_success = artifact.get("success")
    if declared_success is not None and bool(declared_success) is not success:
        raise error(f"{field}.success: declared success contradicts the acceptance evidence")

    if corpus_manifest is not None:
        normalized = validate_corpus_manifest(corpus_manifest)
        if normalized["corpus_manifest_id"] != corpus_manifest_id:
            raise error(f"{field}.corpus_manifest_id: artifact is not bound to this manifest")
        if normalized["corpus_version"] != corpus_version:
            raise error(f"{field}.corpus_version: artifact is not bound to this manifest")
        entry = next(
            (item for item in normalized["entries"] if item["corpus_id"] == corpus_id), None
        )
        if entry is None:
            raise error(f"{field}.corpus_id: {corpus_id!r} is not in the corpus manifest")
        for key, observed in (
            ("profile", profile),
            ("repository_url", repository_url),
            ("repository_revision", repository_revision),
        ):
            if entry[key] != observed:
                raise error(f"{field}.{key}: does not match corpus entry {corpus_id!r}")

    body: dict[str, Any] = {
        "schema_id": RUN_ARTIFACT_SCHEMA,
        "phase_boundary": PHASE_BOUNDARY,
        "corpus_manifest_id": corpus_manifest_id,
        "corpus_version": corpus_version,
        "corpus_id": corpus_id,
        "profile": profile,
        "repository_url": repository_url,
        "repository_revision": repository_revision,
        "aiworkhub_revision": aiworkhub_revision,
        "route": route,
        "model": model,
        "task_id": task_id,
        "request_id": request_id,
        "attempt_id": attempt_id,
        "retries": retries,
        "validation": validation,
        "review": review,
        "usage": usage,
        "platform_claims": platform_claims,
        "platform_evidence": platform_evidence,
        "outcome": outcome,
        "outcome_reason": outcome_reason,
        "accepted_outcome_receipt": receipt,
        "receipt_authentication": receipt_authentication,
        "canonical_authority_state": authority_state,
        "canonical_authority_reason": authority_reason,
        "success": success,
    }
    return _seal(
        body, key="run_artifact_id", provided=artifact.get("run_artifact_id"), error=error
    )


def is_accepted_outcome(
    artifact: Any, *, acceptance_authority: AcceptanceAuthority | None = None
) -> bool:
    """Return True only for a canonically authenticated manager acceptance.

    Without ``acceptance_authority`` there is nothing to authenticate against,
    so the answer is False: an unauthenticated receipt is a recorded claim.
    """
    normalized = validate_run_artifact(artifact, acceptance_authority=acceptance_authority)
    return normalized["success"] is True


def qualification_summary(
    artifacts: list[Any],
    *,
    corpus_manifest: Any,
    minimum_samples_per_profile: int = 3,
    acceptance_authority: AcceptanceAuthority | None = None,
) -> dict[str, Any]:
    """Aggregate run artifacts per profile without inventing missing evidence.

    A profile below ``minimum_samples_per_profile`` stays UNKNOWN rather than
    reporting a rate derived from too few runs, and any attempt with unknown
    cost or tokens makes that profile's observed total UNKNOWN rather than zero.

    ``acceptance_authority`` is threaded down to every row. Without it no row
    can report success, so an aggregate acceptance count can never exceed what
    the canonical authority actually authenticated -- and an acceptance rate
    computed from that population would report "never asked" as ``0.0``, so a
    profile aggregated without a bound authority stays UNKNOWN with the reason
    ``acceptance_authority_not_consulted``.

    One authority is threaded down to *every* row, but a bound authority speaks
    for exactly one attempt. A row belonging to another attempt is therefore
    summarized, not refused: it is recorded in full and counted under
    ``unauthenticated_accepted_outcomes``, and the profile stays UNKNOWN with
    the reason ``acceptance_authority_did_not_cover_every_accepted_row``.
    Aborting the whole summary over it would delete every other row's evidence
    because one neighbour was out of scope.

    Each profile reports two different facts about acceptance. The recorded
    outcome counters partition ``observed_runs`` exactly -- every row carries
    one of ``accepted``/``rejected``/``failed`` -- while
    ``authenticated_accepted_outcomes`` counts only what the canonical authority
    granted, which is always a subset of the recorded acceptances.
    """
    if minimum_samples_per_profile < 1:
        raise ValueError("minimum_samples_per_profile must be at least 1")
    manifest = validate_corpus_manifest(corpus_manifest)
    rows = [
        validate_run_artifact(
            artifact, corpus_manifest=manifest, acceptance_authority=acceptance_authority
        )
        for artifact in artifacts
    ]

    profiles: dict[str, Any] = {}
    for profile in sorted(REQUIRED_PROFILES):
        matched = [row for row in rows if row["profile"] == profile]
        samples = len(matched)

        # What the rows recorded. ``outcome`` is a closed three-value set, so
        # these counters partition the observed population; the check keeps that
        # a checked invariant rather than an assumption a later edit can break.
        recorded = {
            outcome: sum(1 for row in matched if row["outcome"] == outcome)
            for outcome in (OUTCOME_ACCEPTED, OUTCOME_REJECTED, OUTCOME_FAILED)
        }
        if sum(recorded.values()) != samples:
            raise InvalidRunArtifactError(
                f"profiles.{profile}: recorded outcomes must partition observed_runs, "
                f"got {sum(recorded.values())} of {samples}"
            )
        # What the authority granted. Strictly narrower than a recorded
        # acceptance, and never mixed into the partition above.
        authenticated = sum(1 for row in matched if row["success"])

        # A bound authority speaks for exactly one attempt, so a recorded
        # acceptance belonging to a neighbouring attempt is one it was never in
        # a position to grant. Those rows stay recorded and counted here; what
        # they do is leave the acceptance population partly unasked, and an
        # unasked row is not a refused one.
        unasked_acceptances = sum(
            1
            for row in matched
            if row["outcome"] == OUTCOME_ACCEPTED and row["canonical_authority_state"] == UNKNOWN
        )

        if samples < minimum_samples_per_profile:
            state = UNKNOWN
            reason = "no_observed_run" if not samples else "insufficient_samples"
            acceptance_rate: Any = UNKNOWN
        elif acceptance_authority is None:
            # Nothing authenticated any row here, so every row failed closed to
            # ``success: false``. Reporting that as a measured 0.0 would publish
            # an unasked question as a measured refusal.
            state = UNKNOWN
            reason = "acceptance_authority_not_consulted"
            acceptance_rate = UNKNOWN
        elif unasked_acceptances:
            # Same reasoning, one row at a time: a rate computed over a
            # population the authority could only partly speak for reports the
            # unasked rows as refusals it never issued.
            state = UNKNOWN
            reason = "acceptance_authority_did_not_cover_every_accepted_row"
            acceptance_rate = UNKNOWN
        else:
            state = "MEASURED"
            reason = "sufficient_observed_samples"
            acceptance_rate = round(authenticated / samples, 6)

        if not matched or any(not row["usage"]["cost_known"] for row in matched):
            observed_cost: Any = UNKNOWN
            cost_reason = "no_observed_run" if not matched else "one_or_more_attempt_costs_unknown"
        else:
            observed_cost = round(sum(float(row["usage"]["cost_usd"]) for row in matched), 6)
            cost_reason = "complete_observed_cost_population"

        if not matched or any(not row["usage"]["tokens_known"] for row in matched):
            observed_tokens: Any = UNKNOWN
            tokens_reason = (
                "no_observed_run" if not matched else "one_or_more_attempt_token_counts_unknown"
            )
        else:
            observed_tokens = sum(int(row["usage"]["total_tokens"]) for row in matched)
            tokens_reason = "complete_observed_token_population"

        # Every observed row carries an exact retry count, so a total is
        # measured as soon as there is one row to measure. With no observed run
        # there is nothing to sum, and an empty sum is zero only in arithmetic:
        # published beside an UNKNOWN cost and UNKNOWN tokens, a ``0`` reads as
        # an observed profile that happened never to retry.
        if not matched:
            total_retries: Any = UNKNOWN
            retries_reason = "no_observed_run"
        else:
            total_retries = sum(int(row["retries"]) for row in matched)
            retries_reason = "complete_observed_retry_population"

        profiles[profile] = {
            "state": state,
            "reason": reason,
            "observed_runs": samples,
            "recorded_accepted_outcomes": recorded[OUTCOME_ACCEPTED],
            "recorded_rejected_outcomes": recorded[OUTCOME_REJECTED],
            "recorded_failed_outcomes": recorded[OUTCOME_FAILED],
            "authenticated_accepted_outcomes": authenticated,
            "unauthenticated_accepted_outcomes": unasked_acceptances,
            "total_retries": total_retries,
            "total_retries_reason": retries_reason,
            "acceptance_rate": acceptance_rate,
            "observed_cost_usd": observed_cost,
            "observed_cost_reason": cost_reason,
            "observed_total_tokens": observed_tokens,
            "observed_tokens_reason": tokens_reason,
        }

    body: dict[str, Any] = {
        "schema_id": SUMMARY_SCHEMA,
        "phase_boundary": PHASE_BOUNDARY,
        "claim_boundary": CLAIM_BOUNDARY,
        "corpus_manifest_id": manifest["corpus_manifest_id"],
        "corpus_version": manifest["corpus_version"],
        "minimum_samples_per_profile": minimum_samples_per_profile,
        "profiles": profiles,
        "run_artifact_ids": sorted(row["run_artifact_id"] for row in rows),
    }
    return _seal(body, key="summary_id", provided=None, error=InvalidRunArtifactError)


_LLVM_URL = "https://github.com/llvm/llvm-project"
_LLVM_OID = "a4bf6cd7cfb1a1421ba92bca9d017b49936c55e4"

_VSCODE_URL = "https://github.com/microsoft/vscode"
_VSCODE_OID = "912bb683695358a54ae0c670461738984cbb5b95"

_AIRFLOW_URL = "https://github.com/apache/airflow"
_AIRFLOW_OID = "e001b88f5875cfd7e295891a0bbdbc75a3dccbfb"

_GRPC_URL = "https://github.com/grpc/grpc"
_GRPC_OID = "13cecab1c4f45902197a9f8fe4e787eb9c4d4db1"


def _pin(url: str, revision: str, ref: str, ref_object_id: str) -> dict[str, Any]:
    """Record how one revision pin was observed, and where it can be re-read.

    ``ref_object_id`` is what the ref itself advertised: for an annotated tag
    that is the tag object and ``revision`` is the commit it peels to, and for a
    lightweight tag the two are the same. Keeping both makes the observation
    reproducible by anyone who reads the same ref.
    """
    return {
        "observation_method": "git_ls_remote",
        "observed_ref": ref,
        "observed_ref_object_id": ref_object_id,
        "observed_on": _PIN_OBSERVED_ON,
        "locator": f"{url}/commit/{revision}",
    }


# The fixed heterogeneous corpus. Every revision below is a full commit object
# id read from that repository's own remote ref advertisement for a release tag
# -- read-only metadata, never a clone, a build or an execution.
FIXED_CORPUS_ENTRIES: tuple[dict[str, Any], ...] = (
    {
        "corpus_id": "cpp-llvm-project",
        "profile": PROFILE_CPP_LARGE,
        "repository_url": _LLVM_URL,
        "repository_revision": _LLVM_OID,
        "revision_kind": REVISION_KIND,
        "rationale": (
            "LLVM at release llvmorg-19.1.0 is the reference large, expensive C++ build: "
            "a multi-hour native compile, translation units far past what naive whole-file "
            "context can hold, and a toolchain configuration that must be discovered."
        ),
        "pin_evidence": _pin(
            _LLVM_URL,
            _LLVM_OID,
            "refs/tags/llvmorg-19.1.0",
            "4adc0e424295c1744456663d0809a71647321aed",
        ),
    },
    {
        "corpus_id": "ts-vscode",
        "profile": PROFILE_TS_JS_MONOREPO,
        "repository_url": _VSCODE_URL,
        "repository_revision": _VSCODE_OID,
        "revision_kind": REVISION_KIND,
        "rationale": (
            "VS Code at release 1.95.0 is a TypeScript monorepo: many packages behind one "
            "workspace root, cross-package type resolution and build ordering, and lint and "
            "typecheck gates that fail independently of the test gate."
        ),
        "pin_evidence": _pin(
            _VSCODE_URL,
            _VSCODE_OID,
            "refs/tags/1.95.0",
            _VSCODE_OID,
        ),
    },
    {
        "corpus_id": "py-airflow",
        "profile": PROFILE_PYTHON_BACKEND_DATA,
        "repository_url": _AIRFLOW_URL,
        "repository_revision": _AIRFLOW_OID,
        "revision_kind": REVISION_KIND,
        "rationale": (
            "Airflow at release 2.10.0 is a Python backend and data project: a large runtime "
            "dependency surface, database and scheduler state in the test path, and a suite "
            "long enough that test selection is itself part of the problem."
        ),
        "pin_evidence": _pin(
            _AIRFLOW_URL,
            _AIRFLOW_OID,
            "refs/tags/2.10.0",
            "f5e6ac7f8cb4b2576874614808eea012b1857ab9",
        ),
    },
    {
        "corpus_id": "sub-grpc",
        "profile": PROFILE_SUBMODULES_LFS_STRICT_CI,
        "repository_url": _GRPC_URL,
        "repository_revision": _GRPC_OID,
        "revision_kind": REVISION_KIND,
        "rationale": (
            "gRPC at release v1.66.0 carries git submodules that must be materialized before "
            "any build starts, CI that fails closed on formatting and generated-file drift, "
            "and a checkout cost that makes a naive clone-per-attempt untenable."
        ),
        "pin_evidence": _pin(
            _GRPC_URL,
            _GRPC_OID,
            "refs/tags/v1.66.0",
            _GRPC_OID,
        ),
    },
)


def fixed_corpus_manifest() -> dict[str, Any]:
    """Return the checked-in fixed heterogeneous corpus, normalized and sealed.

    This is the repository's corpus, not a candidate list: all four profiles are
    covered, and every entry pins a public repository at an immutable full-length
    commit object id together with the ``pin_evidence`` that recorded the
    observation. It is validated by :func:`validate_corpus_manifest` on every
    call, and at import through :data:`CORPUS_MANIFEST`.
    """
    return validate_corpus_manifest({
        "schema_id": CORPUS_MANIFEST_SCHEMA,
        "corpus_version": CORPUS_VERSION,
        "entries": [
            dict(entry, pin_evidence=dict(entry["pin_evidence"]))
            for entry in FIXED_CORPUS_ENTRIES
        ],
    })


# Validated at import: an unpinned or malformed checked-in corpus is an import
# error here, never a surprise partway through a qualification run.
CORPUS_MANIFEST: dict[str, Any] = fixed_corpus_manifest()
CORPUS_MANIFEST_ID: str = CORPUS_MANIFEST["corpus_manifest_id"]
