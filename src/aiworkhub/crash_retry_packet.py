"""Bounded predecessor-failure evidence for a crash-retried rework launch.

Split out of ``process_launcher`` unchanged under the module size ratchet
(``tests/test_module_size_ratchet.py``). ``process_launcher`` re-imports every
name and keeps ``_materialize_crash_retry_packet`` as a thin wrapper that
injects its own ``_safe_tail`` and ``write_json_0600``, so call sites and
monkeypatches on ``process_launcher`` resolve exactly as before.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from . import attempt_artifacts
from . import worker_workspace as _worker_workspace
from .worker_workspace import WorkerWorkspace, WorkspaceError

MAX_CRASH_RETRY_PACKET_BYTES = 12 * 1024
MAX_CRASH_RETRY_STREAM_BYTES = 2 * 1024
# The sealed attempt review payload already caps the finalizer reason at 500
# characters; the packet never carries more than the bundle holds.
MAX_CRASH_RETRY_TERMINAL_REASON_CHARS = 500


def materialize_crash_retry_packet(
    process_dir: Path,
    workspace: WorkerWorkspace,
    *,
    task_id: str,
    card: Mapping[str, Any],
    rework_overlay_packet: Mapping[str, Any] | None,
    safe_tail: Callable[[Path, int], str],
    write_json_0600: Callable[[Path, Any], None],
) -> tuple[Path | None, dict[str, Any] | None]:
    """Bind bounded predecessor failure evidence to one verified rework overlay.

    The predecessor workspace bytes remain authoritative through the overlay;
    this packet only salvages diagnostics that would otherwise be reread from
    old process logs or re-derived by re-running the declared validation.  A
    crashed predecessor contributes its bounded stream tails.  A predecessor
    that exited cleanly contributes the sealed attempt-artifacts validation
    failure delta instead -- every validation_failed predecessor exits 0, so
    an exit-code gate would drop exactly the runs that hold a measured
    failure -- plus the finalizer's terminal reason when the failure left no
    failed-check receipt (required_output_unchanged, mcp_call_missing, ...).
    Its stream tails are omitted: for an exit-0 run they are the worker's own
    final stream, not diagnostics.  Missing, cross-task, cross-repository, or
    oversized predecessor metadata fails closed by omitting the packet; a
    clean exit that was not measured as a validation failure also omits it,
    because a review-rejected candidate is carried by review_feedback.
    """

    predecessor = card.get("rework_predecessor")
    if not isinstance(predecessor, Mapping) or not isinstance(
        rework_overlay_packet, Mapping
    ):
        return None, None
    request_id = str(predecessor.get("request_id") or "").strip()
    predecessor_task_id = str(predecessor.get("task_id") or task_id).strip()
    if (
        not request_id
        or predecessor_task_id != task_id
        or str(rework_overlay_packet.get("predecessor_request_id") or "")
        != request_id
        or str(rework_overlay_packet.get("predecessor_task_id") or "")
        != task_id
    ):
        raise WorkspaceError("crash_retry_predecessor_identity_mismatch")

    metadata_path = process_dir / f"{request_id}.request.json"
    status_path = process_dir / f"{request_id}.supervisor.json"
    try:
        if (
            metadata_path.is_symlink()
            or status_path.is_symlink()
            or metadata_path.stat().st_size > 1024 * 1024
            or status_path.stat().st_size > 1024 * 1024
        ):
            raise WorkspaceError("crash_retry_predecessor_artifact_unsafe")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"crash_retry_predecessor_artifact_invalid:{exc}") from exc
    if not isinstance(metadata, dict) or not isinstance(status, dict):
        raise WorkspaceError("crash_retry_predecessor_artifact_shape_invalid")
    predecessor_workspace = metadata.get("workspace")
    if (
        str(metadata.get("request_id") or "") != request_id
        or str(metadata.get("task_id") or "") != task_id
        or not isinstance(predecessor_workspace, dict)
    ):
        raise WorkspaceError("crash_retry_predecessor_metadata_identity_mismatch")
    try:
        predecessor_repo = Path(str(predecessor_workspace.get("repo") or "")).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceError("crash_retry_predecessor_repo_invalid") from exc
    if predecessor_repo != workspace.repo.resolve():
        raise WorkspaceError("crash_retry_predecessor_repo_mismatch")

    state = str(status.get("state") or "")
    returncode = status.get("exit_code")
    clean_exit = state == "exited" and returncode == 0
    validation_delta: dict[str, Any] | None = None
    validation_manifest_sha256 = ""
    terminal_substatus = ""
    terminal_reason = ""
    bundle_dir = process_dir / "attempt-artifacts" / request_id
    if bundle_dir.exists():
        try:
            attempt_artifacts.verify_json_bundle(bundle_dir)
            manifest_path = bundle_dir / attempt_artifacts.MANIFEST_FILENAME
            manifest = attempt_artifacts.parse_manifest_json(
                manifest_path.read_text(encoding="utf-8")
            )
            validation_entry = next(
                (entry for entry in manifest.artifacts if entry.role == "validation"),
                None,
            )
            review_entry = next(
                (entry for entry in manifest.artifacts if entry.role == "review"),
                None,
            )
            if review_entry is not None:
                review_payload = json.loads(
                    (bundle_dir / review_entry.path).read_text(encoding="utf-8")
                )
                if isinstance(review_payload, dict):
                    terminal_substatus = str(review_payload.get("target_state") or "")
                    terminal_reason = str(review_payload.get("error") or "")[
                        :MAX_CRASH_RETRY_TERMINAL_REASON_CHARS
                    ]
            if validation_entry is not None:
                validation_payload = json.loads(
                    (bundle_dir / validation_entry.path).read_text(encoding="utf-8")
                )
                checks = (
                    validation_payload.get("checks")
                    if isinstance(validation_payload, dict)
                    else None
                )
                if isinstance(checks, list):
                    validation_delta = _worker_workspace.validation_failure_delta_packet(
                        row for row in checks if isinstance(row, Mapping)
                    )
                    validation_manifest_sha256 = hashlib.sha256(
                        manifest_path.read_bytes()
                    ).hexdigest()
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            attempt_artifacts.InvalidArtifactError,
            attempt_artifacts.InvalidManifestError,
        ) as exc:
            raise WorkspaceError(
                f"crash_retry_validation_artifact_invalid:{exc}"
            ) from exc
    failed_check_count = (
        int(validation_delta.get("failure_count") or 0)
        if isinstance(validation_delta, dict)
        else 0
    )
    measured_validation_failure = (
        failed_check_count > 0 or terminal_substatus == "validation_failed"
    )
    if clean_exit and not measured_validation_failure:
        return None, None
    stdout_path = process_dir / f"{request_id}.stdout.log"
    stderr_path = process_dir / f"{request_id}.stderr.log"
    # The packet is JSON, so JSON encoding already neutralises every
    # metacharacter. The HTML-oriented live-output sanitiser escaped and
    # redacted bytes the successor needs verbatim, so carry the predecessor's
    # diagnostics unescaped and unredacted; the tail hashes below then cover
    # exactly the bytes delivered rather than a pre-sanitised original.
    # A clean exit has no crash diagnostics in its streams -- they are the
    # worker's own final output -- so omit them and keep the packet headroom
    # for the validation delta.
    stream_tails_omitted_reason = "predecessor_exited_clean" if clean_exit else ""
    stdout_tail = (
        "" if clean_exit else safe_tail(stdout_path, MAX_CRASH_RETRY_STREAM_BYTES)
    )
    stderr_tail = (
        "" if clean_exit else safe_tail(stderr_path, MAX_CRASH_RETRY_STREAM_BYTES)
    )
    error = str(status.get("error") or "")[:500]
    if not (stdout_tail or stderr_tail or error or state):
        return None, None

    packet: dict[str, Any] = {
        "schema_id": "aiworkhub.crash_retry_packet.v1",
        "successor_request_id": workspace.request_id,
        "successor_task_id": task_id,
        "predecessor_request_id": request_id,
        "predecessor_task_id": task_id,
        "predecessor_state": state,
        "predecessor_exit_code": returncode,
        "predecessor_error": error,
        "predecessor_terminal_substatus": terminal_substatus,
        # The finalizer's own reason is the only carrier for a failure that
        # left no failed-check receipt; with receipts present it restates them.
        "predecessor_terminal_reason": (
            terminal_reason if failed_check_count == 0 else ""
        ),
        "stream_tails_omitted_reason": stream_tails_omitted_reason,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "stdout_tail_sha256": hashlib.sha256(stdout_tail.encode("utf-8")).hexdigest(),
        "stderr_tail_sha256": hashlib.sha256(stderr_tail.encode("utf-8")).hexdigest(),
        "rework_overlay_sha256": str(
            rework_overlay_packet.get("canonical_digest") or ""
        ),
        "inherited_paths": list(workspace.inherited_rework_paths),
        "validation_failure_delta": validation_delta,
        "validation_manifest_sha256": validation_manifest_sha256,
        "stale_worktree_bytes_authoritative": False,
        "canonical_reread_savings_claimed": False,
    }
    sealed_bytes = _seal_crash_retry_packet(packet)
    paths = packet["inherited_paths"]
    if sealed_bytes > MAX_CRASH_RETRY_PACKET_BYTES and paths:
        # NF-2026-01347: the path list grows with the predecessor (108 paths
        # refused every relaunch of a rework), yet every path and its sha256
        # are already sealed in the rework overlay bound by
        # rework_overlay_sha256.  Carry the list's count and digest plus the
        # overlay pointer so the failure evidence keeps its headroom.
        packet["inherited_paths"] = []
        packet["inherited_paths_manifest"] = {
            "count": len(paths),
            "paths_sha256": hashlib.sha256(
                json.dumps(sorted(paths), separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "home_relative_path": "task_mcp_worker_runtime/rework_overlay.json",
        }
        sealed_bytes = _seal_crash_retry_packet(packet)
    if sealed_bytes > MAX_CRASH_RETRY_PACKET_BYTES:
        raise WorkspaceError(
            f"crash_retry_packet_too_large:{sealed_bytes}>{MAX_CRASH_RETRY_PACKET_BYTES}"
        )
    path = workspace.home / "task_mcp_worker_runtime" / "crash_retry_packet.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json_0600(path, packet)
    return path, packet


def _seal_crash_retry_packet(packet: dict[str, Any]) -> int:
    """Stamp ``packet_sha256`` over the unsealed packet; return the sealed size.

    ``build_worker_prompt`` re-measures the sealed packet against the same cap,
    so bounding only the unsealed bytes let an 83-byte window pass here and
    then refuse the launch there (NF-2026-01347).
    """
    packet.pop("packet_sha256", None)
    canonical = json.dumps(
        packet, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    packet["packet_sha256"] = hashlib.sha256(canonical).hexdigest()
    return len(
        json.dumps(
            packet, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    )
