"""Composite routing identity: (repo_id, thread_id, task_id, event_id).

Enforces strict validation so callbacks and mux state cannot cross
repositories even when thread IDs or task IDs collide. Every field is
bounded by explicit length/character rules; missing repo_id, path
separators, controls, and Unicode confusables are all rejected (fail-
closed) instead of guessed.

Design contract (B811, revised for NF222 v2):
- Immutable, hashable, equatable.
- Canonical serialization (v2): an injective, length-prefixed, versioned
  encoding. Every field is prefixed by its length, and the whole form is
  prefixed by the ``v2`` marker, so distinct field tuples -- even ones with
  adversarial ``:`` placement inside identifier fields -- always yield
  distinct canonical strings, digests, and client-user-message-ids.
- SHA-256 digest is computed over the versioned canonical form for
  deterministic dedup / client-user-message-id.
- Legacy parsing remains explicit and only accepts unambiguous legacy
  forms (exactly the expected number of colon-separated fields with no
  embedded colons); ambiguous colon-containing identities are rejected.
- Repo-scoped path-key helpers that cannot escape their base directory
  and never expose absolute paths in owner-facing callback text.
- Same (thread_id, task_id, event_id) in two different repos produces
  disjoint route keys -- no cross-repo pollution.
- Duplicate event keys (identical composite) are idempotent by construction.
- Malformed or legacy thread-only routing fails closed (``ValueError``).
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

# ---------------------------------------------------------------------------
# Validation rules (fail-closed -- no guessing, no silent truncation)
# ---------------------------------------------------------------------------

# Identifiers: ASCII alphanumeric + limited safe punctuation.
# No path separators ('/', '\\'), no controls (U+0000-U+001F, U+007F-U+009F),
# no Unicode confusables (homoglyphs, zero-width, bidi overrides).
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")

# repo_id: must be present, non-empty, 1-128 chars.
# thread_id: must be present, non-empty, 1-256 chars.
# task_id: must be present, non-empty, 1-256 chars.
# event_id: may be empty (0-128 chars), same character rules when present.
_MAX_REPO_ID_LEN = 128
_MAX_THREAD_ID_LEN = 256
_MAX_TASK_ID_LEN = 256
_MAX_EVENT_ID_LEN = 128
_MAX_PROVIDER_LEN = 32
_MAX_WINDOW_ID_LEN = 128
_MAX_SESSION_ID_LEN = 256
_SUPPORTED_PROVIDERS = frozenset({"codex", "claude", "copilot"})

# Unicode categories that are forbidden in identifiers:
# Cc = control, Cf = format (zero-width, bidi marks), Cs = surrogate,
# Co = private use, Cn = unassigned.
# Also forbid any character whose NFC-normalised form differs from the
# original (catches composed confusables) and any character outside ASCII
# range that resembles an ASCII glyph (homoglyph defence via allowlist:
# only explicit ASCII is permitted).
_FORBIDDEN_CATEGORIES: frozenset[str] = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def _validate_identifier_field(value: str, field_name: str, max_len: int, *, allow_empty: bool = False) -> str:
    """Validate a single identifier field. Returns the stripped value.

    Raises ``ValueError`` on any violation -- never silently truncates or
    coerces.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string, got {type(value).__name__}")

    stripped = value.strip()
    if not stripped:
        if allow_empty:
            return ""
        raise ValueError(f"{field_name} must not be empty")

    if len(stripped) > max_len:
        raise ValueError(
            f"{field_name} exceeds maximum length {max_len} (got {len(stripped)})"
        )

    # Check for forbidden Unicode categories (controls, format chars, etc.)
    for ch in stripped:
        cat = unicodedata.category(ch)
        if cat in _FORBIDDEN_CATEGORIES:
            raise ValueError(
                f"{field_name} contains forbidden character "
                f"U+{ord(ch):04X} (category={cat})"
            )
        # Reject non-ASCII: we only allow explicit ASCII in identifiers.
        if ord(ch) > 127:
            raise ValueError(
                f"{field_name} contains non-ASCII character "
                f"U+{ord(ch):04X} -- only ASCII letters, digits, and "
                f"_.:- are permitted"
            )

    # Check for path separators explicitly (they would pass the regex
    # but must never appear in identifiers).
    if "/" in stripped or "\\" in stripped:
        raise ValueError(f"{field_name} must not contain path separators")

    # Regex final gate.
    if not _IDENTIFIER_RE.fullmatch(stripped):
        raise ValueError(
            f"{field_name} contains invalid characters; "
            f"only [A-Za-z0-9_.:-] are permitted"
        )

    return stripped


# ---------------------------------------------------------------------------
# Injective versioned canonical encoding (NF222 v2)
# ---------------------------------------------------------------------------

_CANONICAL_V2_PREFIX = "v2"


def _encode_length_prefixed(fields: tuple[str, ...]) -> str:
    """Injective, versioned, length-prefixed encoding of an ordered tuple.

    Each field is emitted as ``<len>:<field>`` (the field's character length
    followed by its raw bytes), and the whole body is prefixed with the
    ``v2`` version marker. Because every field length is explicit, embedded
    ``:`` characters inside identifier fields can never make two distinct
    tuples collapse to the same canonical string.
    """
    return _CANONICAL_V2_PREFIX + "".join(f"{len(field)}:{field}" for field in fields)


def _decode_length_prefixed(canonical: str, num_fields: int) -> list[str] | None:
    """Decode a v2 length-prefixed canonical string.

    Returns the ordered fields, or ``None`` when the string is not a valid
    version-2 encoding (wrong marker, malformed/absent length, truncation,
    or trailing data).
    """
    if not canonical.startswith(_CANONICAL_V2_PREFIX):
        return None
    rest = canonical[len(_CANONICAL_V2_PREFIX):]
    fields: list[str] = []
    pos = 0
    for _ in range(num_fields):
        colon = rest.find(":", pos)
        if colon == -1:
            return None
        length_str = rest[pos:colon]
        # Lengths are bounded by the identifier field limits (<=256), so any
        # longer digit run is malformed rather than a legitimate field size.
        if not length_str.isdigit() or len(length_str) > 4:
            return None
        length = int(length_str)
        end = colon + 1 + length
        if end > len(rest):
            return None
        fields.append(rest[colon + 1:end])
        pos = end
    if pos != len(rest):
        return None
    return fields


# ---------------------------------------------------------------------------
# Composite route key
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoRouteKey:
    """Immutable composite routing identity for one callback/mux event.

    Four fields, all validated at construction:
    - ``repo_id``: mandatory, the repository identity (never a path).
    - ``thread_id``: mandatory, the Codex App Server thread id.
    - ``task_id``: mandatory, the AIWorkHub task id.
    - ``event_id``: optional, the outbox event id (empty string = no event).

    Equality, hashing, and serialization are all deterministic and
    byte-exact -- same inputs always produce the same key.
    """

    repo_id: str
    thread_id: str
    task_id: str
    event_id: str = ""

    def __post_init__(self) -> None:
        # Validate via object.__setattr__ because the dataclass is frozen.
        object.__setattr__(self, "repo_id", _validate_identifier_field(self.repo_id, "repo_id", _MAX_REPO_ID_LEN))
        object.__setattr__(
            self, "thread_id", _validate_identifier_field(self.thread_id, "thread_id", _MAX_THREAD_ID_LEN)
        )
        object.__setattr__(self, "task_id", _validate_identifier_field(self.task_id, "task_id", _MAX_TASK_ID_LEN))
        object.__setattr__(
            self, "event_id", _validate_identifier_field(self.event_id, "event_id", _MAX_EVENT_ID_LEN, allow_empty=True)
        )

    def canonical(self) -> str:
        """Injective versioned serialization (v2).

        Length-prefixed and version-marked so two distinct field tuples --
        including ones where an identifier itself contains ``:`` -- can never
        share a canonical string (and therefore never share a digest).
        """
        return _encode_length_prefixed(
            (self.repo_id, self.thread_id, self.task_id, self.event_id)
        )

    def digest(self) -> str:
        """SHA-256 hex digest of ``canonical()``.

        Suitable for deterministic client-user-message-ids and outbox
        dedup keys. 32 hex chars (128 bits of the full 256).
        """
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()[:32]

    def digest_full(self) -> str:
        """Full 64-char SHA-256 hex digest."""
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    def short_id(self) -> str:
        """First 12 hex chars of digest -- compact unique prefix.

        Safe for logging (never exposes full thread/task ids).
        """
        return self.digest()[:12]

    def as_dict(self) -> dict[str, str]:
        """Serialise to plain dict (JSON-safe)."""
        return {
            "repo_id": self.repo_id,
            "thread_id": self.thread_id,
            "task_id": self.task_id,
            "event_id": self.event_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RepoRouteKey:
        """Deserialise from a plain dict. Validates all fields."""
        return cls(
            repo_id=str(d.get("repo_id", "")),
            thread_id=str(d.get("thread_id", "")),
            task_id=str(d.get("task_id", "")),
            event_id=str(d.get("event_id", "")),
        )

    @classmethod
    def parse(cls, canonical: str) -> RepoRouteKey:
        """Parse a ``canonical()`` string back into a ``RepoRouteKey``.

        Accepts the v2 length-prefixed encoding first, then falls back to
        explicit legacy parsing. Legacy parsing only accepts unambiguous
        forms: exactly four colon-separated fields with no embedded colons.
        Any other shape raises ``ValueError``.
        """
        if not isinstance(canonical, str):
            raise ValueError(f"canonical must be a string, got {type(canonical).__name__}")
        fields = _decode_length_prefixed(canonical, 4)
        if fields is not None:
            return cls(
                repo_id=fields[0],
                thread_id=fields[1],
                task_id=fields[2],
                event_id=fields[3],
            )
        parts = canonical.split(":")
        if len(parts) != 4:
            raise ValueError(
                f"canonical form requires exactly 4 colon-separated fields "
                f"(repo:thread:task:event), got {len(parts)}"
            )
        return cls(
            repo_id=parts[0],
            thread_id=parts[1],
            task_id=parts[2],
            event_id=parts[3],
        )

    def to_legacy_dedup_key(self, transition: str, episode_id: str) -> str:
        """Produce a dedup key compatible with the existing
        ``callback_bridge.CallbackEntry.dedup_key`` pattern:
        ``task_id:transition:origin_thread_id:episode_id``.

        The repo_id is NOT embedded in this string (it is still part of
        the RepoRouteKey identity) -- this is a bridge to the existing
        outbox schema, not a replacement. Callers MUST independently
        scope by repo_id (e.g. separate outbox tables per repo).
        """
        return f"{self.task_id}:{transition}:{self.thread_id}:{episode_id}"

    def to_client_user_message_id(self, transition: str, episode_id: str) -> str:
        """Deterministic clientUserMessageId incorporating repo scope.

        Unlike ``callback_bridge.deterministic_client_user_message_id``
        (which lacks repo_id), this one includes the full composite
        identity so two repos with colliding (task, transition, thread,
        episode) tuples still produce distinct ids.
        """
        raw = f"{self.canonical()}:{transition}:{episode_id}"
        return "cbmsg_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    # ------------------------------------------------------------------
    # Repo-scoped path-key helpers (cannot escape base, no absolute paths)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_path_key(key: str) -> str:
        """Validate and normalise a relative path key.

        - Must be a relative path (no leading '/' or drive letter).
        - Must not contain '..' segments.
        - Must not contain null bytes or controls.
        - Returns the stripped, normalised PurePosixPath string.
        """
        if not isinstance(key, str):
            raise ValueError(f"path key must be a string, got {type(key).__name__}")
        stripped = key.strip()
        if not stripped:
            raise ValueError("path key must not be empty")
        if "\x00" in stripped:
            raise ValueError("path key must not contain null bytes")
        for ch in stripped:
            if unicodedata.category(ch) in _FORBIDDEN_CATEGORIES:
                raise ValueError(f"path key contains forbidden character U+{ord(ch):04X}")
        # Must be relative.
        if stripped.startswith("/") or stripped.startswith("\\"):
            raise ValueError("path key must be relative (no leading separator)")
        if len(stripped) >= 2 and stripped[1] == ":":
            raise ValueError("path key must be relative (no drive letter)")
        # No parent traversal.
        segments = stripped.replace("\\", "/").split("/")
        if ".." in segments:
            raise ValueError("path key must not contain '..' segments")
        if "." in segments:
            raise ValueError("path key must not contain '.' segments")
        return PurePosixPath(stripped.replace("\\", "/")).as_posix()

    def repo_scoped_path(self, repo_base: Path, key: str) -> Path:
        """Resolve a repo-relative ``key`` within ``repo_base``.

        The resolved path is guaranteed to be inside ``repo_base``
        (the resolved real path is checked against the resolved real
        path of ``repo_base``). Raises ``ValueError`` if the key
        attempts to escape.

        Returns an absolute ``Path``. Never exposes the absolute path
        in callback/owner text.
        """
        validated_key = self._validate_path_key(key)
        repo_base = repo_base.resolve(strict=False)
        resolved = (repo_base / validated_key).resolve(strict=False)
        # Check containment: resolved must start with repo_base + separator.
        try:
            resolved.relative_to(repo_base)
        except ValueError:
            raise ValueError(
                f"path key {key!r} escapes repo base {repo_base}"
            )
        return resolved

    def repo_scoped_relative(self, repo_base: Path, key: str) -> str:
        """Return the repo-relative POSIX form of ``repo_scoped_path``.

        This is the safe form for callback/owner text: it is always
        relative to the repo root and never exposes the absolute
        filesystem path.
        """
        absolute = self.repo_scoped_path(repo_base, key)
        try:
            return absolute.relative_to(repo_base.resolve(strict=False)).as_posix()
        except ValueError:
            raise ValueError(
                f"path key {key!r} escapes repo base {repo_base}"
            )

    def repo_scoped_safe_repr(self, repo_base: Path, key: str) -> str:
        """Safe representation for owner callback text.

        Returns ``<repo_root>/rel/path`` where ``<repo_root>`` is a
        literal placeholder -- never the real absolute path.
        """
        rel = self.repo_scoped_relative(repo_base, key)
        return f"<repo_root>/{rel}"


@dataclass(frozen=True)
class CoordinatorRouteKey:
    """Strict identity for one coordinator callback route.

    A deliverable route must include repository identity, provider, VS Code
    window identity, task id, and the provider-specific live identity. Codex
    uses ``thread_id``; Claude uses persistent ``session_id``. Partial
    provider-only or thread-only routes fail closed at construction.
    """

    repo_id: str
    provider: str
    window_id: str
    task_id: str
    thread_id: str = ""
    session_id: str = ""
    event_id: str = ""

    def __post_init__(self) -> None:
        repo_id = _validate_identifier_field(self.repo_id, "repo_id", _MAX_REPO_ID_LEN)
        provider = _validate_identifier_field(self.provider, "provider", _MAX_PROVIDER_LEN).lower()
        if provider not in _SUPPORTED_PROVIDERS:
            raise ValueError(f"unsupported callback provider: {provider}")
        window_id = _validate_identifier_field(self.window_id, "window_id", _MAX_WINDOW_ID_LEN)
        task_id = _validate_identifier_field(self.task_id, "task_id", _MAX_TASK_ID_LEN)
        thread_id = _validate_identifier_field(
            self.thread_id, "thread_id", _MAX_THREAD_ID_LEN, allow_empty=True
        )
        session_id = _validate_identifier_field(
            self.session_id, "session_id", _MAX_SESSION_ID_LEN, allow_empty=True
        )
        event_id = _validate_identifier_field(
            self.event_id, "event_id", _MAX_EVENT_ID_LEN, allow_empty=True
        )
        if provider == "codex" and not thread_id:
            raise ValueError("codex callback route requires thread_id")
        if provider == "claude" and not session_id:
            raise ValueError("claude callback route requires session_id")
        if provider == "copilot" and not (thread_id or session_id):
            raise ValueError("copilot callback route requires deferred route identity")

        object.__setattr__(self, "repo_id", repo_id)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "window_id", window_id)
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(self, "thread_id", thread_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "event_id", event_id)

    def canonical(self) -> str:
        return _encode_length_prefixed((
            self.repo_id,
            self.provider,
            self.window_id,
            self.thread_id,
            self.session_id,
            self.task_id,
            self.event_id,
        ))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()[:32]

    def digest_full(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, str]:
        return {
            "repo_id": self.repo_id,
            "provider": self.provider,
            "window_id": self.window_id,
            "thread_id": self.thread_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "event_id": self.event_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CoordinatorRouteKey":
        return cls(
            repo_id=str(d.get("repo_id", "")),
            provider=str(d.get("provider", "")),
            window_id=str(d.get("window_id", "")),
            thread_id=str(d.get("thread_id", "")),
            session_id=str(d.get("session_id", "")),
            task_id=str(d.get("task_id", "")),
            event_id=str(d.get("event_id", "")),
        )

    @classmethod
    def parse(cls, canonical: str) -> "CoordinatorRouteKey":
        """Parse a canonical string back into a ``CoordinatorRouteKey``.

        Accepts the v2 length-prefixed encoding first, then explicit legacy
        parsing that only accepts unambiguous seven-field forms.
        """
        if not isinstance(canonical, str):
            raise ValueError(f"canonical must be a string, got {type(canonical).__name__}")
        fields = _decode_length_prefixed(canonical, 7)
        if fields is not None:
            return cls(
                repo_id=fields[0],
                provider=fields[1],
                window_id=fields[2],
                thread_id=fields[3],
                session_id=fields[4],
                task_id=fields[5],
                event_id=fields[6],
            )
        parts = canonical.split(":")
        if len(parts) != 7:
            raise ValueError(
                "coordinator route requires exactly 7 colon-separated fields "
                "(repo:provider:window:thread:session:task:event)"
            )
        return cls(
            repo_id=parts[0],
            provider=parts[1],
            window_id=parts[2],
            thread_id=parts[3],
            session_id=parts[4],
            task_id=parts[5],
            event_id=parts[6],
        )


# ---------------------------------------------------------------------------
# Factory: reject legacy thread-only routing
# ---------------------------------------------------------------------------

def fail_on_legacy_thread_only(
    thread_id: str,
    *,
    repo_id: str | None = None,
    task_id: str | None = None,
    event_id: str = "",
) -> RepoRouteKey:
    """Construct a ``RepoRouteKey``, or raise ``ValueError`` if repo_id or
    task_id are missing -- legacy thread-only routing is never accepted.

    This is the single entry point callers should use when they have
    potentially partial data (e.g. a thread_id from a sideband registry
    but no repo_id yet). It fails closed: missing repo_id or task_id is
    a hard error, never a fallback to thread-only routing.
    """
    if not repo_id:
        raise ValueError(
            "repo_id is required for composite routing; "
            "legacy thread-only routing is not supported"
        )
    if not task_id:
        raise ValueError(
            "task_id is required for composite routing; "
            "legacy thread-only routing is not supported"
        )
    return RepoRouteKey(
        repo_id=repo_id,
        thread_id=thread_id,
        task_id=task_id,
        event_id=event_id,
    )


def fail_on_incomplete_coordinator_route(
    *,
    repo_id: str | None = None,
    provider: str | None = None,
    window_id: str | None = None,
    task_id: str | None = None,
    thread_id: str = "",
    session_id: str = "",
    event_id: str = "",
) -> CoordinatorRouteKey:
    """Construct a full coordinator route or reject partial routing."""

    if not repo_id:
        raise ValueError("repo_id is required for coordinator callback routing")
    if not provider:
        raise ValueError("provider is required for coordinator callback routing")
    if not window_id:
        raise ValueError("window_id is required for coordinator callback routing")
    if not task_id:
        raise ValueError("task_id is required for coordinator callback routing")
    return CoordinatorRouteKey(
        repo_id=repo_id,
        provider=provider,
        window_id=window_id,
        thread_id=thread_id,
        session_id=session_id,
        task_id=task_id,
        event_id=event_id,
    )
