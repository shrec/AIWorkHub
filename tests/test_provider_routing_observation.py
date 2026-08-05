from scripts import check_provider_routing_observation


def test_checked_in_provider_routing_observation_recomputes_exactly() -> None:
    assert check_provider_routing_observation.check() == []
