import json

from aiworkhub import core, server

_REROUTE_ARGS = {
    "from_runner": "codex_gpt-5.3-codex-spark",
    "to_runner": "codex_gpt-5.5",
    "to_adapter_id": "codex_cli",
    "to_model": "gpt-5.5",
    "reason": "operational retry route repaired",
    "topic": "nf01688",
}
_CARD_PADDING = "x" * 55_000


def _core_result():
    card = {
        "task_id": "T_REROUTE",
        "status": "pending",
        "worker_status": "unclaimed",
        "runner": "codex_gpt-5.5",
        "padding": _CARD_PADDING,
    }
    return {
        "ok": True,
        "returncode": 0,
        "stdout": json.dumps(card),
        "command": ["aiworkhub", "reroute"],
        "from_runner": "codex_gpt-5.3-codex-spark",
        "to_runner": "codex_gpt-5.5",
        "to_model": "gpt-5.5",
    }


def _patch_core(monkeypatch):
    calls = []

    def reroute(**kwargs):
        calls.append(kwargs)
        return _core_result()

    monkeypatch.setattr(core, "reroute_launch_identity", reroute)
    return calls


def test_reroute_default_reply_is_receipt_without_card_body(monkeypatch):
    calls = _patch_core(monkeypatch)

    result = server.aiworkhub_task_reroute_launch_identity("T_REROUTE", **_REROUTE_ARGS)

    assert result["schema_id"] == "aiworkhub.reroute_launch_identity_receipt.v1"
    assert result["ok"] is True
    assert result["task_id"] == "T_REROUTE"
    assert result["status"] == "pending"
    assert result["worker_status"] == "unclaimed"
    assert result["card_sha256"]
    assert result["card_bytes"] > 55_000
    assert result["from_runner"] == "codex_gpt-5.3-codex-spark"
    assert result["to_runner"] == "codex_gpt-5.5"
    assert result["to_model"] == "gpt-5.5"
    assert result["include_card"] == "none"
    assert "card" not in result
    assert "stdout" not in result
    assert _CARD_PADDING not in json.dumps(result)
    assert len(json.dumps(result)) < 5_000
    assert calls == [{"task_id": "T_REROUTE", **_REROUTE_ARGS}]


def test_reroute_include_card_full_returns_the_card(monkeypatch):
    calls = _patch_core(monkeypatch)

    result = server.aiworkhub_task_reroute_launch_identity(
        "T_REROUTE", include_card="full", **_REROUTE_ARGS
    )

    assert result["include_card"] == "full"
    assert result["card"]["padding"] == _CARD_PADDING
    assert result["card"]["runner"] == "codex_gpt-5.5"
    assert "include_card" not in calls[0]


def test_reroute_invalid_include_card_is_refused_before_core(monkeypatch):
    calls = _patch_core(monkeypatch)

    result = server.aiworkhub_task_reroute_launch_identity(
        "T_REROUTE", include_card="everything", **_REROUTE_ARGS
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_include_card"
    assert calls == []
