"""Compact ``aiworkhub.validation_failed_packet.v1`` for a validation_failed card.

Diagnosing a red card otherwise needs task_show/review_packet (10-40 KB). The
packet keeps only what a manager reads: the failing command, its exit code and
role, the failing node ids, a short output tail and a pointer to the full log.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from . import core

SCHEMA_ID = "aiworkhub.validation_failed_packet.v1"
MAX_PACKET_BYTES = 4096
_TAIL_MAX_BYTES = 2048
_TAIL_MIN_BYTES = 256
_NODEIDS_MAX = 10
_NODEIDS_CAPPED = 3
_COMMAND_MAX_BYTES = 512
_NODEID_MAX_BYTES = 256
_LOG_MANIFEST_PARTS = (
    ".aiworkhub", "runtime", "process_logs", "processes", "attempt-artifacts",
)
_NODEID_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)


def canonical_json(packet: dict[str, Any]) -> str:
    return json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _packet_bytes(packet: dict[str, Any]) -> int:
    return len(canonical_json(packet).encode("utf-8"))


def _last_bytes(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[-limit:].decode("utf-8", errors="ignore")


def _first_bytes(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


def _terminal_substatus(card: dict[str, Any]) -> str:
    direct = card.get("terminal_substatus")
    if direct:
        return str(direct)
    review = card.get("terminal_review")
    if isinstance(review, dict):
        return str(review.get("substatus") or "")
    return ""


def _failing_row(row: dict[str, Any], index: int, roles: list[Any]) -> dict[str, Any]:
    stderr_tail = str(row.get("stderr_tail") or "")
    stdout_tail = str(row.get("stdout_tail") or "")
    nodeids: list[str] = []
    for match in _NODEID_RE.finditer(stdout_tail + "\n" + stderr_tail):
        nodeid = _first_bytes(match.group(1), _NODEID_MAX_BYTES)
        if nodeid not in nodeids:
            nodeids.append(nodeid)
        if len(nodeids) >= _NODEIDS_MAX:
            break
    role = row.get("behavioral_role")
    if not role and index < len(roles):
        role = roles[index]
    command = str(row.get("declared_command") or row.get("command") or "")
    duration = row.get("duration_s")
    if duration is None:
        duration = row.get("duration")
    return {
        "command": _first_bytes(command, _COMMAND_MAX_BYTES),
        "role": role or None,
        "exit_code": row.get("returncode"),
        "duration_s": duration,
        "timed_out": bool(row.get("timed_out")),
        "failing_nodeids": nodeids,
        "tail": _last_bytes("\n".join(part for part in (stderr_tail, stdout_tail) if part), _TAIL_MAX_BYTES),
    }


def _log_ref(request_id: str, repo_root: str | Path | None) -> dict[str, Any]:
    relative = "/".join((*_LOG_MANIFEST_PARTS, request_id, "manifest.json"))
    digest = None
    if repo_root is not None and request_id:
        manifest = Path(repo_root).joinpath(*_LOG_MANIFEST_PARTS, request_id, "manifest.json")
        try:
            if manifest.is_file():
                digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        except OSError:
            digest = None
    return {"path": relative, "sha256": digest}


def _bound(packet: dict[str, Any], tails: list[str]) -> dict[str, Any]:
    """Shrink ``packet`` to MAX_PACKET_BYTES: halve tails, cap node ids, drop tails, drop rows."""

    failing = packet["failing"]
    if _packet_bytes(packet) <= MAX_PACKET_BYTES:
        return packet
    packet["truncated"] = True
    limit = _TAIL_MAX_BYTES
    while limit > _TAIL_MIN_BYTES and _packet_bytes(packet) > MAX_PACKET_BYTES:
        limit = max(_TAIL_MIN_BYTES, limit // 2)
        for entry, tail in zip(failing, tails):
            entry["tail"] = _last_bytes(tail, limit)
    if _packet_bytes(packet) > MAX_PACKET_BYTES:
        for entry in failing:
            entry["failing_nodeids"] = entry["failing_nodeids"][:_NODEIDS_CAPPED]
    if _packet_bytes(packet) > MAX_PACKET_BYTES:
        for entry in failing:
            entry["tail"] = ""
    omitted = 0
    while len(failing) > 1 and _packet_bytes(packet) > MAX_PACKET_BYTES:
        failing.pop()
        omitted += 1
        packet["failing_omitted"] = omitted
    return packet


def build_packet(card: dict[str, Any], repo_root: str | Path | None = None) -> dict[str, Any] | None:
    if not isinstance(card, dict) or _terminal_substatus(card) != "validation_failed":
        return None
    review = card.get("terminal_review")
    review = review if isinstance(review, dict) else {}
    evidence = review.get("evidence")
    return packet_from_evidence(
        task_id=card.get("task_id"),
        request_id=str(review.get("request_id") or ""),
        evidence=evidence if isinstance(evidence, dict) else {},
        roles=card.get("validation_roles"),
        repo_root=repo_root,
    )


def packet_from_evidence(
    *,
    task_id: Any,
    request_id: str,
    evidence: dict[str, Any],
    roles: Any,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """The packet from terminal evidence alone (also used before the card is written)."""
    validation = evidence.get("validation")
    rows = [row for row in validation if isinstance(row, dict)] if isinstance(validation, list) else []
    roles = roles if isinstance(roles, list) else []
    failing: list[dict[str, Any]] = []
    tails: list[str] = []
    for index, row in enumerate(rows):
        if row.get("timed_out") or row.get("returncode") not in (0, None):
            entry = _failing_row(row, index, roles)
            failing.append(entry)
            tails.append(entry["tail"])
    hashes = evidence.get("changed_path_hashes")
    packet: dict[str, Any] = {
        "schema_id": SCHEMA_ID,
        "task_id": task_id,
        "request_id": request_id or None,
        "candidate_sha256": core._evidence_identity(hashes)[1] if hashes is not None else None,
        "failing": failing,
        "passing_count": len(rows) - len(failing),
        "log_ref": _log_ref(request_id, repo_root),
    }
    # NF-2026-01922: a validation_failed that reaches the manager says how many
    # mechanical auto-returns this card already had.
    auto_rework = evidence.get("validation_auto_rework")
    if isinstance(auto_rework, dict):
        # An unreadable history is unknown (None), never reported as zero.
        prior = auto_rework.get("prior_auto_returns")
        packet["prior_auto_returns"] = prior if isinstance(prior, int) and not isinstance(prior, bool) else None
        packet["auto_rework_decision"] = str(auto_rework.get("cause") or "")[:64] or None
    return _bound(packet, tails)
