"""One VS Code LM bridge worker tool call, extracted from ``ProcessManager``.

``ProcessManager._invoke_vscode_lm_worker_tool_bound`` is a thin delegation to
:func:`invoke_vscode_lm_worker_tool_bound` below (NF-2026-01729:
``process_launcher.py`` sat exactly on the module size ratchet).  The move is
byte-for-byte apart from the four-space dedent a method-to-function move
forces; ``self`` is still the
:class:`ProcessManager` instance, so every ``self.`` collaborator resolves
exactly as before.  Module level collaborators are re-bound from the
``process_launcher`` module object on entry rather than imported here, because
the test suite monkeypatches that module and a name captured at import time
would leave those patches pointing at code this function no longer calls --
silently, with the tests still green.
"""

from __future__ import annotations

from typing import Any

__all__ = ["VSCODE_LM_WORKER_TOOL_SEAM_NAMES", "invoke_vscode_lm_worker_tool_bound"]

# The function's complete runtime free-variable set on ``process_launcher``.
# ``Any`` appears only in annotations, which are never evaluated, so it is
# resolved from this module's own import and is deliberately NOT re-bound.
VSCODE_LM_WORKER_TOOL_SEAM_NAMES: tuple[str, ...] = (
    "ALLOW_WRITES_ENV",
    "Path",
    "WORKER_BRIDGE_AUTHORIZED_PROCESS_STATES",
    "hashlib",
    "json",
    "os",
    "re",
    "replace",
    "runtime_adapters",
    "split_lifecycle_tail",
    "stat",
    "worker_ai_tools_mcp",
)


def invoke_vscode_lm_worker_tool_bound(
    self,
    request_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """Run one GLM bridge tool through the exact task-scoped worker authority.

    The VS Code Language Model API can only call tools through the
    extension-owned coordinator MCP connection.  This bridge keeps that
    transport while executing the call with the launched worker's
    immutable identity and HMAC audit ledger, so completion gates observe
    genuine worker tool use instead of an unrelated manager-side call.
    """
    # Every module-level name this function used while it lived in
    # ``process_launcher`` is re-bound here from that exact module object, so a
    # ``monkeypatch.setattr(process_launcher, name, ...)`` seam still reaches the
    # code below.  The list is ``VSCODE_LM_WORKER_TOOL_SEAM_NAMES`` and is
    # asserted against the body by
    # ``test_process_launcher_extraction_seams_nf01729.py``.
    from . import process_launcher as _pl

    ALLOW_WRITES_ENV = _pl.ALLOW_WRITES_ENV
    Path = _pl.Path
    WORKER_BRIDGE_AUTHORIZED_PROCESS_STATES = _pl.WORKER_BRIDGE_AUTHORIZED_PROCESS_STATES
    hashlib = _pl.hashlib
    json = _pl.json
    os = _pl.os
    re = _pl.re
    replace = _pl.replace
    runtime_adapters = _pl.runtime_adapters
    split_lifecycle_tail = _pl.split_lifecycle_tail
    stat = _pl.stat
    worker_ai_tools_mcp = _pl.worker_ai_tools_mcp

    if not re.fullmatch(r"[a-f0-9]{32}", str(request_id or "")):
        return {"ok": False, "reason": "worker_bridge_request_id_invalid"}
    if not isinstance(tool_input, dict):
        return {"ok": False, "reason": "worker_bridge_input_invalid"}
    if len(json.dumps(tool_input, ensure_ascii=False).encode("utf-8")) > 16 * 1024:
        return {"ok": False, "reason": "worker_bridge_input_too_large"}
    # NF389: bind the bridge's authenticated provider-call identity into the
    # exact worker audit context. A PRESENT value -- even an explicit empty
    # string -- must pass the same fail-closed validator the ledger uses;
    # only an ABSENT key retains the backward-compatible empty sentinel.
    # ``provenance`` is provider-call provenance ONLY for the Source Graph
    # dispatch (where ``source_graph_query`` would otherwise default it to
    # ``live``). Every other tool keeps its own write-intent ``provenance``
    # field untouched in tool_input (a distinct concept).
    provider_call_id = ""
    provenance = ""
    is_source_graph = tool_name == "aiworkhub_manager_source_graph_query"
    # NF389 sealed correction: consume (remove) the authenticated identity
    # from the tool input here so it can never leak into the
    # ``source_graph_query(ctx, **tool_input)`` dispatch (which would raise
    # ``TypeError``), while still binding the validated value into the exact
    # worker audit context below.
    if "provider_call_id" in tool_input:
        raw_provider_call_id = tool_input.pop("provider_call_id")
        try:
            provider_call_id = worker_ai_tools_mcp.validate_provider_call_id(
                raw_provider_call_id
            )
        except worker_ai_tools_mcp.WorkerToolError as exc:
            return {"ok": False, "reason": str(exc)}
    if is_source_graph and "provenance" in tool_input:
        raw_provenance = tool_input.pop("provenance")
        try:
            provenance = worker_ai_tools_mcp.validate_provenance(raw_provenance)
        except worker_ai_tools_mcp.WorkerToolError as exc:
            return {"ok": False, "reason": str(exc)}
    events = self._request_events(request_id)
    if not events:
        return {"ok": False, "reason": "worker_bridge_request_not_found"}
    latest = split_lifecycle_tail(events)[0]
    vscode_lm_adapters = {
        runtime_adapters.VSCODE_LM_ADAPTER,
        runtime_adapters.GLM_VSCODE_LM_ADAPTER,
        runtime_adapters.DEEPSEEK_VSCODE_LM_ADAPTER,
    }
    if latest and latest.get("adapter_id") not in vscode_lm_adapters:
        return {"ok": False, "reason": "worker_bridge_adapter_mismatch"}
    if latest.get("state") not in WORKER_BRIDGE_AUTHORIZED_PROCESS_STATES:
        return {"ok": False, "reason": "worker_bridge_request_not_active"}
    metadata_path = self._metadata_from_events(events)
    if metadata_path is None or metadata_path.parent.resolve() != self.process_dir.resolve():
        return {"ok": False, "reason": "worker_bridge_metadata_invalid"}
    try:
        if metadata_path.is_symlink() or not metadata_path.is_file() or metadata_path.stat().st_size > 2 * 1024 * 1024:
            return {"ok": False, "reason": "worker_bridge_metadata_invalid"}
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "reason": "worker_bridge_metadata_unreadable"}
    if (
        str(metadata.get("request_id") or "") != request_id
        or str(metadata.get("adapter_id") or "") not in vscode_lm_adapters
    ):
        return {"ok": False, "reason": "worker_bridge_metadata_identity_mismatch"}
    worker_meta = metadata.get("worker_mcp") or {}
    workspace_meta = metadata.get("workspace") or {}
    rework_overlay_packet: dict[str, Any] | None = None
    rework_overlay_packet_path: Path | None = None
    source_graph_authority = worker_meta.get("source_graph_authority")
    if (
        isinstance(source_graph_authority, dict)
        and source_graph_authority.get("authority_source") == "rework_overlay"
    ):
        try:
            workspace_home = Path(str(workspace_meta["home"])).resolve()
            expected_path = (
                workspace_home / "task_mcp_worker_runtime" / "rework_overlay.json"
            )
            declared_path = Path(str(source_graph_authority["packet_path"]))
            if os.path.normcase(os.path.abspath(declared_path)) != os.path.normcase(
                os.path.abspath(expected_path)
            ):
                return {
                    "ok": False,
                    "reason": "worker_bridge_rework_overlay_path_mismatch",
                }
            if declared_path.is_symlink():
                return {
                    "ok": False,
                    "reason": "worker_bridge_rework_overlay_symlink_forbidden",
                }
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(declared_path, flags)
            try:
                file_stat = os.fstat(fd)
                if not stat.S_ISREG(file_stat.st_mode):
                    return {
                        "ok": False,
                        "reason": "worker_bridge_rework_overlay_not_regular",
                    }
                if file_stat.st_size > worker_ai_tools_mcp.MAX_REWORK_OVERLAY_PACKET_BYTES:
                    return {
                        "ok": False,
                        "reason": "worker_bridge_rework_overlay_too_large",
                    }
                packet_buffer = bytearray()
                remaining = file_stat.st_size + 1
                while remaining > 0:
                    chunk = os.read(fd, min(remaining, 64 * 1024))
                    if not chunk:
                        break
                    packet_buffer.extend(chunk)
                    remaining -= len(chunk)
            finally:
                os.close(fd)
            packet_bytes = bytes(packet_buffer)
            if len(packet_bytes) != file_stat.st_size:
                return {
                    "ok": False,
                    "reason": "worker_bridge_rework_overlay_changed_during_read",
                }
            rework_overlay_packet = json.loads(packet_bytes.decode("utf-8"))
            if not isinstance(rework_overlay_packet, dict):
                return {
                    "ok": False,
                    "reason": "worker_bridge_rework_overlay_invalid",
                }
            worker_ai_tools_mcp._verify_rework_overlay_packet(
                rework_overlay_packet,
                str(metadata["task_id"]),
                request_id,
                str(metadata["runner"]),
                Path(str(worker_meta["authority_repo"])).resolve(),
            )
            if (
                str(source_graph_authority.get("target_request_id") or "")
                != str(rework_overlay_packet.get("predecessor_request_id") or "")
                or str(source_graph_authority.get("target_task_id") or "")
                != str(rework_overlay_packet.get("predecessor_task_id") or "")
                or str(source_graph_authority.get("packet_sha256") or "")
                != str(rework_overlay_packet.get("canonical_digest") or "")
            ):
                return {
                    "ok": False,
                    "reason": "worker_bridge_rework_overlay_metadata_mismatch",
                }
            rework_overlay_packet_path = declared_path
        except KeyError:
            return {
                "ok": False,
                "reason": "worker_bridge_rework_overlay_metadata_missing",
            }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {
                "ok": False,
                "reason": "worker_bridge_rework_overlay_unreadable",
            }
        except worker_ai_tools_mcp.WorkerToolError as exc:
            return {
                "ok": False,
                "reason": "worker_bridge_rework_overlay_verification_failed:"
                + str(exc)[:240],
            }
    try:
        ctx = worker_ai_tools_mcp.WorkerToolContext(
            task_id=str(metadata["task_id"]),
            runner=str(metadata["runner"]),
            topic=str(metadata["topic"]),
            request_id=request_id,
            repo=Path(str(workspace_meta["path"])).resolve(),
            authority_repo=Path(str(worker_meta["authority_repo"])).resolve(),
            source_graph_targets=tuple(str(value) for value in worker_meta.get("source_graph_targets") or []),
            allowed_writes=tuple(str(value) for value in worker_meta.get("allowed_writes") or []),
            session_topic=str(worker_meta.get("session_topic") or metadata["topic"]),
            audit_ledger_path=Path(str(worker_meta["audit_ledger_path"])),
            audit_hmac_key_path=Path(str(worker_meta["audit_hmac_key_path"])),
            quality_review_packet_path=(
                Path(str((metadata.get("quality_review") or {})["packet_path"]))
                if isinstance(metadata.get("quality_review"), dict)
                else None
            ),
            rework_overlay_packet=rework_overlay_packet,
            rework_overlay_packet_path=rework_overlay_packet_path,
            provider_call_id=provider_call_id,
            provenance=provenance,
        )
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "reason": "worker_bridge_context_invalid"}

    if tool_name in {
        "aiworkhub_worker_semantic_edit_prepare",
        "aiworkhub_worker_semantic_edit_apply",
    }:
        if tool_name.endswith("_apply") and os.environ.get(ALLOW_WRITES_ENV, "0") != "1":
            return {"ok": False, "reason": "worker_bridge_write_gate_closed"}
        identity = replace(ctx, provider_call_id="", provenance="")
        cached = self._vscode_lm_semantic_sessions.get(request_id)
        if cached is not None and cached[0] != identity:
            self._vscode_lm_semantic_sessions.pop(request_id, None)
            return {"ok": False, "reason": "worker_bridge_semantic_identity_changed"}
        if cached is None:
            session = worker_ai_tools_mcp.WorkerSemanticEditSession(ctx)
            self._vscode_lm_semantic_sessions[request_id] = (identity, session)
        else:
            session = cached[1]
            # The immutable request identity is unchanged; only this call's
            # authenticated provider provenance changes under the request lock.
            session.ctx = ctx
        if tool_name.endswith("_prepare"):
            return session.prepare(
                file_path=tool_input.get("file_path") or tool_input.get("path", ""),
                start_line=tool_input.get("start_line", 0),
                end_line=tool_input.get("end_line", 0),
                include_fragment=tool_input.get("include_fragment") is True,
                # NF-2026-01533: absent anchors stay None, so a bridge that
                # sends none keeps the unanchored contract.
                expected_first_line=tool_input.get("expected_first_line"),
                expected_last_line=tool_input.get("expected_last_line"),
            )
        # NF-2026-01373: emulated models omit idempotency_key even when named;
        # it only dedupes an identical replay, so derive it from that pair.
        target_id, new = tool_input.get("target_id", ""), tool_input.get("new")
        key = tool_input.get("idempotency_key") or "derived:" + hashlib.sha256(
            f"{target_id}\0{new}".encode("utf-8")).hexdigest()
        return session.apply(
            target_id=target_id, new=new, idempotency_key=key,
            allow_escape_level_change=tool_input.get(
                "allow_escape_level_change", False
            ),
        )
    if tool_name == "aiworkhub_manager_source_graph_query":
        identity = replace(ctx, provider_call_id="", provenance="")
        cached = self._vscode_lm_semantic_sessions.get(request_id)
        if cached is not None and cached[0] != identity:
            self._vscode_lm_semantic_sessions.pop(request_id, None)
            return {"ok": False, "reason": "worker_bridge_semantic_identity_changed"}
        if cached is None:
            editor = worker_ai_tools_mcp.WorkerSemanticEditSession(ctx)
            self._vscode_lm_semantic_sessions[request_id] = (identity, editor)
        else:
            editor = cached[1]
            editor.ctx = ctx
        result = worker_ai_tools_mcp.source_graph_query(ctx, **tool_input)
        editor.note_source_graph_delivery(result)
        return result
    if tool_name == "aiworkhub_manager_semantic_edit_prepare":
        return worker_ai_tools_mcp.WorkerSemanticEditSession(ctx).prepare(
            # Text-only VS Code LM providers occasionally mirror the final
            # edit envelope's ``path`` field even though the private tool
            # schema calls it ``file_path``.  Both names carry the same
            # repository-relative authority; normalize at the bridge and
            # leave all scope/hash checks to the semantic-edit session.
            file_path=tool_input.get("file_path") or tool_input.get("path", ""),
            start_line=tool_input.get("start_line", 0),
            end_line=tool_input.get("end_line", 0),
            expected_first_line=tool_input.get("expected_first_line"),
            expected_last_line=tool_input.get("expected_last_line"),
        )
    if tool_name == "aiworkhub_manager_session_current_state":
        return worker_ai_tools_mcp.session_current_state(ctx, limit=tool_input.get("limit", 12))
    if tool_name == "aiworkhub_manager_ai_memory_search":
        return worker_ai_tools_mcp.ai_memory_search(
            ctx, query=tool_input.get("query", ""), limit=tool_input.get("limit", 8)
        )
    if tool_name == "aiworkhub_manager_kb_search":
        return worker_ai_tools_mcp.kb_search(
            ctx, query=tool_input.get("query", ""), limit=tool_input.get("limit", 8)
        )
    if tool_name == "aiworkhub_manager_kb_get":
        return worker_ai_tools_mcp.kb_get(ctx, key=tool_input.get("key", ""))
    if tool_name == "aiworkhub_manager_kb_related":
        return worker_ai_tools_mcp.kb_related(ctx, key=tool_input.get("key", ""))
    if tool_name == "aiworkhub_manager_session_write_intent":
        return worker_ai_tools_mcp.session_write_intent(
            ctx,
            action=tool_input.get("action", ""),
            content=tool_input.get("content", ""),
            idempotency_key=tool_input.get("idempotency_key", ""),
            provenance=tool_input.get("provenance", ""),
        )
    if tool_name == "aiworkhub_manager_ai_memory_write_intent":
        return worker_ai_tools_mcp.ai_memory_write_intent(
            ctx,
            action=tool_input.get("action", ""),
            key=tool_input.get("key", ""),
            value=tool_input.get("value", ""),
            tags=tool_input.get("tags", ""),
            scope=tool_input.get("scope", "project"),
            idempotency_key=tool_input.get("idempotency_key", ""),
            provenance=tool_input.get("provenance", ""),
        )
    if tool_name == "aiworkhub_manager_kb_write_intent":
        return worker_ai_tools_mcp.kb_write_intent(
            ctx,
            action=tool_input.get("action", ""),
            key=tool_input.get("key", ""),
            title=tool_input.get("title", ""),
            body=tool_input.get("body", ""),
            category=tool_input.get("category", ""),
            tags=tool_input.get("tags", ""),
            source_refs=tool_input.get("source_refs", ""),
            replacement_key=tool_input.get("replacement_key", ""),
            idempotency_key=tool_input.get("idempotency_key", ""),
            provenance=tool_input.get("provenance", ""),
        )
    # A read on the way IN, so the reviewer prompt's ban on submission
    # tools does not reach it.  The packet path is bound server-side from
    # the request metadata above; the prompt tells the reviewer to call
    # this with no arguments and forbids supplying a path or identity
    # (quality_reviewer.py:578-582), so ``tool_input`` is deliberately NOT
    # forwarded -- a provider can neither redirect the read nor break it
    # by mirroring a stray field.
    if tool_name == "aiworkhub_worker_quality_review_packet_read":
        return worker_ai_tools_mcp.quality_review_packet_read(ctx)
    if tool_name == "aiworkhub_worker_quality_review_submit":
        findings = tool_input.get("findings", [])
        if not isinstance(findings, list):
            return {"ok": False, "reason": "worker_bridge_findings_invalid"}
        return worker_ai_tools_mcp.quality_review_submit(
            ctx,
            packet_sha256=tool_input.get("packet_sha256", ""),
            lens=tool_input.get("lens", ""),
            findings=findings,
        )
    return {"ok": False, "reason": "worker_bridge_tool_not_allowed"}
