import json

import pytest

from aiworkhub import vscode_lm_bridge
from aiworkhub.vscode_lm_worker import run


def test_vscode_lm_diagnostics_bound_nf01309(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec_path = tmp_path / "spec.json"
    response_path = tmp_path / "response.json"
    marker = "NF01309_LATEST_TURN_TRACE_MARKER"

    rows = [
        "row-{:02d}-".format(index) + ("x" * 350) + (marker if index == 15 else "")
        for index in range(16)
    ]

    response = {
        "schema_id": vscode_lm_bridge.RESPONSE_SCHEMA_ID,
        "request_id": "nf01309-request",
        "error": "semantic_edit_stage_rejected:anchor_mismatch",
        "diagnostics": {
            "protocol_preview": "{}",
            "turn_trace": rows,
        },
    }
    response_path.write_text(json.dumps(response), encoding="utf-8")
    spec = {
        "schema_id": "aiworkhub.vscode_lm.worker_spec.v1",
        "workspace_path": str(workspace),
        "response_path": str(response_path),
        "request_id": "nf01309-request",
        "timeout_seconds": 30,
    }
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(RuntimeError) as excinfo:
        run(spec_path)

    message = str(excinfo.value)
    assert message.startswith("vscode_lm_request_failed:")
    _prefix, payload = message.split(":diagnostics=", 1)
    assert len(payload) <= 2048
    diagnostics = json.loads(payload)
    assert marker in diagnostics["turn_trace"][-1]
