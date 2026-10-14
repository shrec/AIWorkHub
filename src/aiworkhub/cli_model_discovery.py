"""Discover CLI model identities from provider-owned state, never a live call.

Codex CLI keeps its own list of currently-offered models in
``models_cache.json`` under ``CODEX_HOME``; that cache is this repository's
only source of truth for which Codex model slugs exist right now, so a
hardcoded catalog row silently drifts from what the provider actually offers
the moment the provider changes its lineup. Claude Code resolves its own
aliases (``opus``, ``sonnet``, ...) to a concrete model id internally, but an
alias is not guaranteed to be the newest model of its family, so the hub pins
each alias to an explicit id (``CLAUDE_CLI_PINNED_MODELS``) instead of passing
the alias on ``--model``. The id a real run actually reported is only ever
knowable from the first line of its ``stream-json`` output (see
:mod:`aiworkhub.manager_loop_backends`); it is recorded here as observed
evidence rather than re-derived.

Nothing in this module spawns a process, opens a socket, or otherwise asks a
provider anything -- every function reads at most a file this repository or a
CLI already wrote, and a missing or unreadable file is a normal, silent state
rather than an error.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import platform_io

# Measured on Claude Code 2.1.280: ``opus`` resolved to claude-opus-5-5, but
# ``sonnet`` to claude-sonnet-5 and ``haiku`` to a 4.5 model, so a family alias
# does not deliver the newest family. Every alias is pinned to an explicit id
# verified live on that CLI; the alias itself stays the stored and picker value.
CLAUDE_CLI_PINNED_MODELS: Mapping[str, str] = MappingProxyType(
    {
        "opus": "claude-opus-5-5",
        "sonnet": "claude-sonnet-5-5",
        "haiku": "claude-haiku-5-5",
        "fable": "claude-fable-5-1",
    }
)
CLAUDE_CLI_ALIASES: tuple[str, ...] = tuple(CLAUDE_CLI_PINNED_MODELS)

_CODEX_MODELS_CACHE_NAME = "models_cache.json"
_CLAUDE_RESOLUTIONS_RELATIVE_PATH = Path(".aiworkhub/runtime/claude_model_resolutions.json")
_MAX_CACHE_BYTES = 2_000_000


def _codex_home(home: Path | str | None) -> Path:
    if home is not None:
        return Path(home)
    override = os.environ.get("CODEX_HOME", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".codex"


def codex_models(home: Path | str | None = None) -> list[dict[str, Any]]:
    """Listed Codex models from the CLI's own cache, in its priority order.

    Reads only ``<CODEX_HOME>/models_cache.json``. A missing, oversized or
    unparseable cache yields no rows -- the CLI has simply not run there yet
    -- and never raises.
    """

    path = _codex_home(home) / _CODEX_MODELS_CACHE_NAME
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_CACHE_BYTES:
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        return []
    listed: list[dict[str, Any]] = []
    for entry in raw_models:
        if not isinstance(entry, dict) or entry.get("visibility") != "list":
            continue
        slug = str(entry.get("slug") or "").strip()
        if not slug:
            continue
        priority = entry.get("priority")
        listed.append({
            "model": slug,
            "label": str(entry.get("display_name") or slug).strip(),
            "priority": priority if isinstance(priority, int) and not isinstance(priority, bool) else None,
        })
    listed.sort(key=lambda item: (0, item["priority"]) if isinstance(item["priority"], int) else (1, 0))
    return listed


def _resolutions_path(repo_root: Path | str) -> Path:
    return Path(repo_root).resolve() / _CLAUDE_RESOLUTIONS_RELATIVE_PATH


def _read_resolutions(repo_root: Path | str) -> dict[str, Any]:
    path = _resolutions_path(repo_root)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_CACHE_BYTES:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def record_claude_resolution(repo_root: Path | str, alias: str, resolved: str) -> None:
    """Persist the model id ``alias`` resolved to on the CLI's last real run.

    Silently a no-op for anything that is not one of ``CLAUDE_CLI_ALIASES``
    or carries no resolved id, so a manager turn on a pinned, non-alias model
    never touches disk here.
    """

    alias = str(alias or "").strip()
    resolved = str(resolved or "").strip()
    if alias not in CLAUDE_CLI_ALIASES or not resolved:
        return
    resolutions = _read_resolutions(repo_root)
    resolutions[alias] = {
        "resolved": resolved,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    encoded = json.dumps(resolutions, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path = _resolutions_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
        platform_io.durable_atomic_replace(tmp_name, path)
        tmp_name = ""
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def claude_models(repo_root: Path | str) -> list[dict[str, Any]]:
    """Every Claude CLI alias, labelled with the explicit id the hub pins it to.

    Always returns all of ``CLAUDE_CLI_ALIASES``. ``model`` stays the bare
    alias -- the value the Manager picker stores and launches -- while the
    label names the pinned id that launch runs. ``repo_root`` is kept for
    callers only: a recorded resolution is observed evidence, never the label.
    """

    return [
        {"model": alias, "label": f"{pinned} ({alias})"}
        for alias, pinned in CLAUDE_CLI_PINNED_MODELS.items()
    ]


def claude_init_model(stdout_path: Path) -> str:
    """First ``{"type": "system", "subtype": "init"}`` event's model, if any.

    Reads at most the CLI's first 64 stdout lines; a missing, truncated or
    corrupt stream yields "" rather than raising.
    """

    try:
        with stdout_path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(64):
                line = handle.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if (
                    isinstance(event, dict)
                    and event.get("type") == "system"
                    and event.get("subtype") == "init"
                ):
                    return str(event.get("model") or "")
    except OSError:
        pass
    return ""
