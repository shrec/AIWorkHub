"""NF-2026-02257: a UI router/link change is not an authority boundary."""

from aiworkhub.quality_evidence import derive_risk_signals

CARD = {"task_type": "code", "validation": ["pytest"]}


def test_flutter_router_and_link_change_is_not_authority_boundary() -> None:
    signals = derive_risk_signals(
        CARD,
        [
            "lib/router.dart",
            "lib/features/account/account_screen.dart",
            "lib/routes/settings_routes.dart",
        ],
    )
    assert "authority_boundary" not in signals


def test_route_path_token_still_marks_authority_boundary() -> None:
    for path in ("src/aiworkhub/manager_route.py", "src/routing/table.py"):
        assert "authority_boundary" in derive_risk_signals(CARD, [path]), path


def test_other_authority_markers_unchanged() -> None:
    assert "authority_boundary" in derive_risk_signals(CARD, ["src/app/policy.py"])
